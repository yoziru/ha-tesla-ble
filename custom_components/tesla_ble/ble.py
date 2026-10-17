"""BLE transport over Home Assistant's bluetooth stack.

Works with local adapters and ESPHome Bluetooth proxies alike:
``bluetooth.async_ble_device_from_address`` returns whatever HA resolved,
and ``bleak_retry_connector`` routes the connection accordingly.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING

from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BleakNotFoundError,
    establish_connection,
)
from homeassistant.exceptions import HomeAssistantError

import tesla_ble

from .const import READ_UUID, WRITE_CHUNK_SIZE, WRITE_UUID

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

CONNECT_ATTEMPTS = 3


class TeslaBleTransport:
    """Owns the GATT connection; frames are queued and written by the manager."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        name: str,
        on_disconnect: Callable[[], None],
    ) -> None:
        self._hass = hass
        self._address = address
        self._name = name
        self._on_disconnect = on_disconnect
        self._client: BleakClientWithServiceCache | None = None
        self._write_queue: deque[bytes] = deque()
        self._notify_callback: Callable[[bytes], None] | None = None
        self._write_lock = asyncio.Lock()

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    def set_notify_callback(self, callback: Callable[[bytes], None] | None) -> None:
        self._notify_callback = callback

    async def connect(self) -> None:
        from homeassistant.components import bluetooth

        device: BLEDevice | None = bluetooth.async_ble_device_from_address(self._hass, self._address, connectable=True)
        if device is None:
            raise BleakNotFoundError(f"Device {self._address} is not currently visible")

        _LOGGER.debug("Connecting to %s (%s)", self._address, device.name)
        connection = asyncio.create_task(
            establish_connection(
                BleakClientWithServiceCache,
                device,
                self._name,
                max_attempts=CONNECT_ATTEMPTS,
                disconnected_callback=self._handle_disconnected,
            )
        )
        try:
            client = await asyncio.shield(connection)
        except asyncio.CancelledError:
            # The connector owns the client until it returns it. Join it before
            # disposal so cancellation cannot strand an established link.
            try:
                client = await connection
            except Exception:
                pass
            else:
                await self._dispose_client(client)
            raise
        self._client = client
        try:
            await client.start_notify(
                READ_UUID,
                lambda sender, data: self._handle_notification(sender, data) if self._client is client else None,
            )
            if not self.is_connected:
                raise ConnectionError("Disconnected while subscribing to vehicle notifications")
        except BaseException:
            if self._client is client:
                self._client = None
            await self._dispose_client(client)
            raise
        _LOGGER.debug("Connected to %s", self._address)

    async def disconnect(self) -> None:
        client, self._client = self._client, None
        self._write_queue.clear()
        if client is None:
            return
        await self._dispose_client(client)
        _LOGGER.debug("Disconnected from %s", self._address)

    async def _dispose_client(self, client: BleakClientWithServiceCache) -> None:
        with suppress(Exception):
            await client.stop_notify(READ_UUID)
        with suppress(Exception):
            await client.disconnect()

    def queue_write(self, data: bytes) -> None:
        for offset in range(0, len(data), WRITE_CHUNK_SIZE):
            self._write_queue.append(data[offset : offset + WRITE_CHUNK_SIZE])

    async def async_flush(self) -> None:
        async with self._write_lock:
            client = self._client
            if client is None:
                self._write_queue.clear()
                return
            while self._write_queue and self._client is client:
                chunk = self._write_queue.popleft()
                try:
                    await client.write_gatt_char(WRITE_UUID, chunk, response=False)
                except Exception as err:
                    _LOGGER.debug("GATT write failed; dropping connection", exc_info=True)
                    self._handle_disconnected(client)
                    await self._dispose_client(client)
                    raise HomeAssistantError("Vehicle GATT write failed") from err

    def _handle_notification(self, _sender: object, data: bytearray) -> None:
        if self._notify_callback is not None:
            self._notify_callback(bytes(data))

    def _handle_disconnected(self, client: object) -> None:
        if self._client is None or self._client is not client:
            return
        _LOGGER.debug("Disconnected from %s", self._address)
        self._client = None
        self._write_queue.clear()
        self._on_disconnect()


class HaBleAdapter(tesla_ble.BleAdapter):
    """TeslaBLE BleAdapter that hands frames to the transport's write queue."""

    def __init__(self, transport: TeslaBleTransport) -> None:
        super().__init__()
        self._transport = transport

    def connect(self, address: str) -> None:
        # Connection lifecycle is owned by the manager.
        pass

    def disconnect(self) -> None:
        pass

    def write(self, data: bytes) -> bool:
        if not self._transport.is_connected:
            return False
        self._transport.queue_write(bytes(data))
        return True
