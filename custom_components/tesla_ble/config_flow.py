"""Config and options flow for the Tesla BLE integration."""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from typing import Any

import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError

import tesla_ble

from .const import (
    CONF_ADDRESS,
    CONF_DISCONNECT_DELAY,
    CONF_INFOTAINMENT_POLL_INTERVAL_ACTIVE,
    CONF_INFOTAINMENT_POLL_INTERVAL_AWAKE,
    CONF_INFOTAINMENT_SLEEP_TIMEOUT,
    CONF_PAIR_ON_SETUP,
    CONF_ROLE,
    CONF_VCSEC_POLL_INTERVAL,
    CONF_VIN,
    DEFAULT_DISCONNECT_DELAY,
    DEFAULT_INFOTAINMENT_POLL_INTERVAL_ACTIVE,
    DEFAULT_INFOTAINMENT_POLL_INTERVAL_AWAKE,
    DEFAULT_INFOTAINMENT_SLEEP_TIMEOUT,
    DEFAULT_VCSEC_POLL_INTERVAL,
    DOMAIN,
    MAX_DISCONNECT_DELAY,
    MAX_INFOTAINMENT_POLL_INTERVAL,
    MAX_SLEEP_TIMEOUT,
    MAX_VCSEC_POLL_INTERVAL,
    MIN_DISCONNECT_DELAY,
    MIN_INFOTAINMENT_POLL_INTERVAL,
    MIN_SLEEP_TIMEOUT,
    MIN_VCSEC_POLL_INTERVAL,
    ROLE_LABELS,
    SERVICE_UUID,
    TESLA_BEACON_DATA_PREFIX,
    TESLA_BEACON_MANUFACTURER_ID,
)

VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")

MANUAL_ADDRESS = "__manual__"

# A vehicle must keep advertising across this settle window before discovery
# offers it, so a car merely passing by does not create a setup flow.
DISCOVERY_SETTLE_INTERVAL = 10
DISCOVERY_SETTLE_CHECKS = 3

# The car may take minutes to accept the key (the whitelist request itself
# retries for about three minutes), so the repair flow waits for a verified
# session instead of the car's status reply.
PAIRING_CONFIRMATION_TIMEOUT = 240
# Before asking to pair, check whether the car already accepts the existing
# key; that needs no key-card tap and usually answers within one poll.
PAIRING_PROBE_TIMEOUT = 30

VIN_SCHEMA = vol.Schema({vol.Required(CONF_VIN): str})


def _normalize_address(value: str) -> str:
    return value.strip().upper().replace("-", ":")


def _is_tesla_advert(info: BluetoothServiceInfoBleak) -> bool:
    """Whether an advertisement that matched a discovery matcher is a Tesla.

    Accepts the advertised (VIN-hash or "Tesla <vin suffix>") name scheme,
    the GATT service UUID when a vehicle exposes it in its advertisement, or
    the VCSEC iBeacon signature that passive-only scanners still forward.
    """
    if tesla_ble.is_tesla_vehicle_name(info.name or ""):
        return True
    if SERVICE_UUID in info.service_uuids:
        return True
    beacon = info.manufacturer_data.get(TESLA_BEACON_MANUFACTURER_ID, b"")
    return beacon.startswith(TESLA_BEACON_DATA_PREFIX)


class TeslaBLEConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a Tesla vehicle, discovered over Bluetooth or entered manually."""

    _repair_task: asyncio.Task | None = None

    VERSION = 1
    MINOR_VERSION = 2

    def __init__(self) -> None:
        self._address: str | None = None
        self._advertised_name: str | None = None
        self._vin: str | None = None
        self._role = "DRIVER"
        self._discovered: dict[str, str] = {}

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> ConfigFlowResult:
        """A Bluetooth advertisement matched a Tesla discovery matcher."""
        if not _is_tesla_advert(discovery_info):
            return self.async_abort(reason="not_supported")
        if any(
            entry.data.get(CONF_ADDRESS) == discovery_info.address
            for entry in self._async_current_entries(include_ignore=False)
        ):
            return self.async_abort(reason="already_configured")
        if not await self._async_still_advertising(discovery_info.address):
            return self.async_abort(reason="not_seen")
        self._address = discovery_info.address
        self._advertised_name = (
            discovery_info.name if tesla_ble.is_tesla_vehicle_name(discovery_info.name or "") else None
        )
        # The address identifies the vehicle until its VIN is known. HA needs a
        # unique ID on the flow for "Ignore" to work on the discovery card.
        await self.async_set_unique_id(discovery_info.address, raise_on_progress=False)
        self.context["title_placeholders"] = {"name": discovery_info.name or "Tesla vehicle"}
        return await self.async_step_vin()

    async def _async_still_advertising(self, address: str) -> bool:
        """Require the vehicle to keep advertising across a settle window."""
        for _ in range(DISCOVERY_SETTLE_CHECKS):
            await asyncio.sleep(DISCOVERY_SETTLE_INTERVAL)
            info = bluetooth.async_last_service_info(self.hass, address, connectable=True)
            if info is None or time.monotonic() - info.time > DISCOVERY_SETTLE_INTERVAL * 2:
                return False
        return True

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick a discovered vehicle or enter its address manually."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            if address == MANUAL_ADDRESS:
                return await self.async_step_manual()
            self._address = address
            self._advertised_name = self._discovered.get(address)
            await self.async_set_unique_id(address, raise_on_progress=False)
            return await self.async_step_vin()

        configured_addresses = {
            entry.data.get(CONF_ADDRESS) for entry in self._async_current_entries(include_ignore=False)
        }
        for info in bluetooth.async_discovered_service_info(self.hass):
            if info.address in configured_addresses or not info.name:
                continue
            if not tesla_ble.is_tesla_vehicle_name(info.name):
                continue
            self._discovered[info.address] = info.name

        if not self._discovered:
            return await self.async_step_manual()

        choices = {address: f"{name} ({address})" for address, name in sorted(self._discovered.items())}
        choices[MANUAL_ADDRESS] = "Enter Bluetooth address manually"
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(choices)}),
        )

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manual Bluetooth address entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            address = _normalize_address(user_input[CONF_ADDRESS])
            if not MAC_RE.match(address):
                errors[CONF_ADDRESS] = "invalid_address"
            else:
                self._address = address
                self._advertised_name = None
                return await self.async_step_vin()
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): str}),
            errors=errors,
        )

    async def async_step_vin(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the VIN and validate it against the advertised name."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {
            "expected_name": "",
            "advertised_name": self._advertised_name or "",
            "address": self._address or "",
        }

        if user_input is None and self._advertised_name is None and self._address is not None:
            # Look up the currently advertised name for a manual address.
            device = bluetooth.async_ble_device_from_address(self.hass, self._address, connectable=True)
            if device is not None and tesla_ble.is_tesla_vehicle_name(device.name or ""):
                self._advertised_name = device.name
                placeholders["advertised_name"] = device.name

        if user_input is not None:
            vin = user_input[CONF_VIN].strip().upper()
            if not VIN_RE.match(vin):
                errors[CONF_VIN] = "invalid_vin"
            else:
                await self.async_set_unique_id(vin)
                self._abort_if_unique_id_configured()
                for existing in self._async_current_entries(include_ignore=False):
                    if existing.data.get(CONF_ADDRESS) == self._address:
                        return self.async_abort(reason="already_configured")
                placeholders["expected_name"] = tesla_ble.get_vin_advertisement_name(vin)
                if self._advertised_name and not tesla_ble.matches_vin(self._advertised_name, vin):
                    errors[CONF_VIN] = "name_mismatch"
                if not errors:
                    self._vin = vin
                    return await self.async_step_role()

        return self.async_show_form(
            step_id="vin",
            data_schema=VIN_SCHEMA,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_role(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose the permissions requested when enrolling the vehicle key."""
        if user_input is not None:
            self._role = user_input[CONF_ROLE]
            return await self.async_step_pair()
        return self.async_show_form(
            step_id="role",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ROLE, default="DRIVER"): vol.In(
                        {k: v for k, v in ROLE_LABELS.items() if k != "OWNER"}
                    )
                }
            ),
        )

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Explain pairing; the entry pairs automatically once it is added."""
        if user_input is not None:
            assert self._vin is not None and self._address is not None
            return self.async_create_entry(
                title=f"Tesla {self._vin[-6:]}",
                data={
                    CONF_VIN: self._vin,
                    CONF_ADDRESS: self._address,
                    CONF_ROLE: self._role,
                    CONF_PAIR_ON_SETUP: True,
                },
            )
        return self.async_show_form(
            step_id="pair", data_schema=vol.Schema({}), description_placeholders={"role": ROLE_LABELS[self._role]}
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return TeslaBLEOptionsFlow()

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            address = _normalize_address(user_input[CONF_ADDRESS])
            if not MAC_RE.fullmatch(address):
                errors[CONF_ADDRESS] = "invalid_address"
            elif any(
                current.entry_id != entry.entry_id and current.data.get(CONF_ADDRESS) == address
                for current in self._async_current_entries(include_ignore=False)
            ):
                return self.async_abort(reason="already_configured")
            else:
                device = bluetooth.async_ble_device_from_address(self.hass, address, connectable=True)
                if (
                    device
                    and tesla_ble.is_tesla_vehicle_name(device.name or "")
                    and not tesla_ble.matches_vin(device.name, entry.data[CONF_VIN])
                ):
                    errors[CONF_ADDRESS] = "name_mismatch"
                else:
                    if entry.update_listeners:
                        return self.async_update_and_abort(entry, data_updates={CONF_ADDRESS: address})
                    return self.async_update_reload_and_abort(entry, data_updates={CONF_ADDRESS: address})
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS, default=entry.data[CONF_ADDRESS]): str}),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        if (manager := getattr(entry, "runtime_data", None)) is not None and manager.paired:
            return self.async_update_and_abort(entry, data_updates={})
        if user_input is not None:
            return await self.async_step_reauth_pair()
        return self.async_show_form(step_id="reauth_confirm", data_schema=vol.Schema({}))

    async def _async_repair_pairing(self) -> bool:
        """Repair pairing; a verified session with the car is the only confirmation."""
        entry = self._get_reauth_entry()
        manager = getattr(entry, "runtime_data", None)
        if manager is None:
            return False
        manager.begin_repair()
        # The car may already accept the existing key; verify before asking the
        # user to tap a key card again.
        if await self._async_wait_for_pairing(manager, PAIRING_PROBE_TIMEOUT):
            manager.end_repair()
            return True
        with contextlib.suppress(HomeAssistantError):
            await manager.async_pair()
        # Start a fresh window; the whitelist request may still be retrying
        # after the car confirmed, so polling verifies whether the key is
        # accepted instead of trusting the car's status reply.
        manager.begin_repair()
        paired = await self._async_wait_for_pairing(manager, PAIRING_CONFIRMATION_TIMEOUT)
        manager.end_repair()
        return paired

    async def _async_wait_for_pairing(self, manager: Any, timeout: float) -> bool:
        """Wait for a verified session; give up early when the car rejects the key."""
        deadline = self.hass.loop.time() + timeout
        while self.hass.loop.time() < deadline:
            if manager.paired:
                return True
            if manager.repair_rejection_seen and not manager.pairing_command_pending:
                return False
            await asyncio.sleep(0.1)
        return manager.paired

    async def async_step_reauth_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._repair_task is None:
            self._repair_task = self.hass.async_create_task(self._async_repair_pairing())
        if not self._repair_task.done():
            return self.async_show_progress(
                step_id="reauth_pair", progress_action="pairing", progress_task=self._repair_task
            )
        return self.async_show_progress_done(next_step_id="reauth_finish")

    async def async_step_reauth_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._repair_task.result():
            return self.async_update_and_abort(self._get_reauth_entry(), data_updates={})
        self._repair_task = None
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=vol.Schema({}), errors={"base": "pairing_failed"}
        )


class TeslaBLEOptionsFlow(OptionsFlow):
    """Polling and connection tuning."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_VCSEC_POLL_INTERVAL,
                    default=options.get(CONF_VCSEC_POLL_INTERVAL, DEFAULT_VCSEC_POLL_INTERVAL),
                ): vol.All(int, vol.Range(min=MIN_VCSEC_POLL_INTERVAL, max=MAX_VCSEC_POLL_INTERVAL)),
                vol.Optional(
                    CONF_INFOTAINMENT_POLL_INTERVAL_AWAKE,
                    default=options.get(
                        CONF_INFOTAINMENT_POLL_INTERVAL_AWAKE, DEFAULT_INFOTAINMENT_POLL_INTERVAL_AWAKE
                    ),
                ): vol.All(int, vol.Range(min=MIN_INFOTAINMENT_POLL_INTERVAL, max=MAX_INFOTAINMENT_POLL_INTERVAL)),
                vol.Optional(
                    CONF_INFOTAINMENT_POLL_INTERVAL_ACTIVE,
                    default=options.get(
                        CONF_INFOTAINMENT_POLL_INTERVAL_ACTIVE, DEFAULT_INFOTAINMENT_POLL_INTERVAL_ACTIVE
                    ),
                ): vol.All(int, vol.Range(min=MIN_INFOTAINMENT_POLL_INTERVAL, max=MAX_INFOTAINMENT_POLL_INTERVAL)),
                vol.Optional(
                    CONF_INFOTAINMENT_SLEEP_TIMEOUT,
                    default=options.get(CONF_INFOTAINMENT_SLEEP_TIMEOUT, DEFAULT_INFOTAINMENT_SLEEP_TIMEOUT),
                ): vol.All(int, vol.Range(min=MIN_SLEEP_TIMEOUT, max=MAX_SLEEP_TIMEOUT)),
                vol.Optional(
                    CONF_DISCONNECT_DELAY,
                    default=options.get(CONF_DISCONNECT_DELAY, DEFAULT_DISCONNECT_DELAY),
                ): vol.All(int, vol.Range(min=MIN_DISCONNECT_DELAY, max=MAX_DISCONNECT_DELAY)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
