"""Full-stack tests: the integration manager driving the C++ Vehicle bindings.

A FakeGattClient replaces the Bluetooth transport: frames written by the
bindings are fed to a SimulatedVehicle, whose replies are pushed back through
the notification callback. No real Bluetooth is involved.
"""

from __future__ import annotations

import asyncio
import base64
import gc
import hashlib
import time
import weakref
from datetime import timedelta
from types import SimpleNamespace

import pytest
import tesla_ble
from conftest import TEST_PRIVATE_KEY_PEM
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.tesla_ble import ble as ble_module
from custom_components.tesla_ble import manager as manager_module
from custom_components.tesla_ble.const import CONF_ADDRESS, CONF_PAIR_ON_SETUP, CONF_VIN, DOMAIN
from custom_components.tesla_ble.update import TeslaFirmwareUpdate

VIN = "5YJ30123456789ABC"
ADDRESS = "AA:BB:CC:DD:EE:FF"


class FakeGattClient:
    """Assembles length-prefixed frames and answers with the simulated car."""

    def __init__(self, sim: tesla_ble.SimulatedVehicle) -> None:
        self.sim = sim
        self.is_connected = True
        self._buffer = bytearray()
        self._notify = None
        self._on_disconnect = None
        self.frames = []
        self.active_writes = 0
        self.max_active_writes = 0

    async def start_notify(self, uuid, callback) -> None:
        await asyncio.sleep(0)
        self._notify = callback

    async def stop_notify(self, uuid) -> None:
        self._notify = None

    async def write_gatt_char(self, uuid, chunk, response: bool = False) -> None:
        self.active_writes += 1
        self.max_active_writes = max(self.max_active_writes, self.active_writes)
        await asyncio.sleep(0)
        self.active_writes -= 1
        self._buffer.extend(chunk)
        while len(self._buffer) >= 2:
            length = int.from_bytes(self._buffer[:2], "big")
            if len(self._buffer) < 2 + length:
                break
            frame = bytes(self._buffer[: 2 + length])
            self.frames.append(frame)
            del self._buffer[: 2 + length]
            for reply in self.sim.on_client_frame(frame):
                self._notify(None, bytearray(reply))

    async def disconnect(self) -> None:
        self.is_connected = False
        if self._on_disconnect:
            self._on_disconnect(self)


async def setup_entry(
    hass: HomeAssistant,
    sim: tesla_ble.SimulatedVehicle,
    monkeypatch,
    hass_storage,
    with_key: bool = True,
    pair_on_setup: bool = False,
    reachable: bool = True,
) -> tuple:
    client = FakeGattClient(sim)

    async def establish(_class, _device, _name, **kwargs):
        if not reachable:
            raise HomeAssistantError("test: vehicle not reachable")
        client.is_connected = True
        client._on_disconnect = kwargs["disconnected_callback"]
        return client

    from homeassistant.components import bluetooth

    monkeypatch.setattr(
        bluetooth,
        "async_ble_device_from_address",
        lambda *a, **kw: SimpleNamespace(address=ADDRESS, name="Tesla", rssi=-64),
    )
    monkeypatch.setattr(
        bluetooth,
        "async_last_service_info",
        lambda *a, **kw: SimpleNamespace(address=ADDRESS, name="Tesla", rssi=-64, time=time.monotonic()),
    )
    monkeypatch.setattr(ble_module, "establish_connection", establish)

    data = {CONF_VIN: VIN, CONF_ADDRESS: ADDRESS}
    if pair_on_setup:
        data[CONF_PAIR_ON_SETUP] = True
    entry = MockConfigEntry(domain=DOMAIN, data=data, unique_id=VIN)
    if with_key:
        key = TEST_PRIVATE_KEY_PEM.encode() + b"\x00"
        hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {
            "version": 1,
            "data": {"private_key": base64.b64encode(key).decode()},
        }
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, client


async def settle(hass: HomeAssistant, ticks: int = 6) -> None:
    """Fire manager ticks so queued frames are processed and replied to."""
    now = dt_util.utcnow()
    for _ in range(ticks):
        now += timedelta(seconds=1)
        async_fire_time_changed(hass, now)
        await hass.async_block_till_done(wait_background_tasks=True)


async def settle_until(hass: HomeAssistant, condition, max_ticks: int = 200) -> None:
    """Fire ticks until `condition()` is true."""
    now = dt_util.utcnow()
    for _ in range(max_ticks):
        if condition():
            return
        now += timedelta(seconds=1)
        async_fire_time_changed(hass, now)
        await hass.async_block_till_done(wait_background_tasks=True)
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached within the tick budget")


def state_of(hass: HomeAssistant, platform: str, unique_id: str) -> str | None:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
    assert entity_id, f"entity {platform}/{unique_id} not found"
    state = hass.states.get(entity_id)
    return None if state is None else state.state


def distance_in_miles(hass: HomeAssistant, platform: str, unique_id: str) -> float:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
    state = hass.states.get(entity_id)
    value = float(state.state)
    if state.attributes.get("unit_of_measurement") == "km":
        value /= 1.609344
    return value


async def test_poll_updates_entities(hass: HomeAssistant, sim, monkeypatch, hass_storage, enable_bluetooth) -> None:
    entry, _client = await setup_entry(hass, sim, monkeypatch, hass_storage)
    manager = entry.runtime_data
    manager._next_vcsec_poll = 0

    await settle_until(hass, lambda: manager.charge_state and manager.vehicle_status and manager.drive_state)

    # User-visible entity ids stay stable (translation keys resolve to names).
    assert hass.states.get("sensor.tesla_789abc_battery") is not None
    assert state_of(hass, "sensor", f"{VIN}_sensor_battery_level") == "55"
    assert state_of(hass, "sensor", f"{VIN}_sensor_charger_power") == "unknown"
    assert distance_in_miles(hass, "sensor", f"{VIN}_sensor_odometer") == pytest.approx(1234.5, abs=0.1)
    assert state_of(hass, "sensor", f"{VIN}_sensor_charging_state") == "Charging"
    assert state_of(hass, "binary_sensor", f"{VIN}_binary_asleep") == "off"
    assert state_of(hass, "binary_sensor", f"{VIN}_binary_charging") == "on"
    assert state_of(hass, "binary_sensor", f"{VIN}_binary_connection") == "on"
    assert state_of(hass, "switch", f"{VIN}_switch_charging") == "on"
    assert state_of(hass, "number", f"{VIN}_number_charge_limit") == "80"
    assert state_of(hass, "lock", f"{VIN}_lock_doors") == "locked"
    signal_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{VIN}_sensor_ble_signal")
    assert er.async_get(hass).async_get(signal_id).disabled_by == er.RegistryEntryDisabler.INTEGRATION
    assert manager.connected is True


async def test_lock_service_completes(hass: HomeAssistant, sim, monkeypatch, hass_storage, enable_bluetooth) -> None:
    entry, _client = await setup_entry(hass, sim, monkeypatch, hass_storage)
    manager = entry.runtime_data
    manager._next_vcsec_poll = 0
    await settle_until(hass, lambda: bool(manager.vehicle_status))

    lock_id = er.async_get(hass).async_get_entity_id("lock", DOMAIN, f"{VIN}_lock_doors")
    await hass.services.async_call("lock", "lock", {"entity_id": lock_id}, blocking=True)
    await settle_until(hass, lambda: manager._vehicle.pending_command_count == 0)

    assert state_of(hass, "sensor", f"{VIN}_sensor_last_command") == "Lock: ok"
    assert state_of(hass, "lock", f"{VIN}_lock_doors") == "locked"

    manager._on_closures_state({"locked": True})
    manager._on_vehicle_status({"locked": False})
    assert state_of(hass, "lock", f"{VIN}_lock_doors") == "unlocked"


async def test_charging_switch_completes(hass: HomeAssistant, sim, monkeypatch, hass_storage, enable_bluetooth) -> None:
    sim.set_charging_state(tesla_ble.charging_state_tag("Disconnected"))
    entry, _client = await setup_entry(hass, sim, monkeypatch, hass_storage)
    manager = entry.runtime_data
    manager._next_vcsec_poll = 0
    await settle_until(hass, lambda: bool(manager.charge_state))

    switch_id = er.async_get(hass).async_get_entity_id("switch", DOMAIN, f"{VIN}_switch_charging")
    await hass.services.async_call("switch", "turn_on", {"entity_id": switch_id}, blocking=True)
    await settle_until(hass, lambda: manager._vehicle.pending_command_count == 0)

    assert state_of(hass, "sensor", f"{VIN}_sensor_last_command") == "Start Charging: ok"


async def test_pair_button_persists_key(hass: HomeAssistant, sim, monkeypatch, hass_storage, enable_bluetooth) -> None:
    monkeypatch.setattr(
        "custom_components.tesla_ble.button.TeslaPairButton._attr_entity_registry_enabled_default", True
    )
    entry, _client = await setup_entry(hass, sim, monkeypatch, hass_storage, with_key=False)
    manager = entry.runtime_data
    button_id = er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{VIN}_button_pair")
    await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)
    await settle_until(hass, lambda: manager._vehicle.pending_command_count == 0)

    assert state_of(hass, "sensor", f"{VIN}_sensor_last_command") == "Pair: ok"
    assert manager._store.load("private_key") is not None


@pytest.mark.parametrize("sleeping", [False, True])
async def test_force_update_button(
    hass: HomeAssistant, sim, monkeypatch, hass_storage, enable_bluetooth, sleeping
) -> None:
    entry, _client = await setup_entry(hass, sim, monkeypatch, hass_storage)
    manager = entry.runtime_data
    manager._next_vcsec_poll = 0
    await settle_until(hass, lambda: bool(manager.charge_state) and manager._vehicle.pending_command_count == 0)
    await manager.async_set_polling(False)
    if sleeping:
        sim.set_sleeping(True)
        manager._vehicle.vcsec_poll()
        await manager._transport.async_flush()
        await settle_until(
            hass, lambda: manager.vehicle_status.get("asleep") and manager._vehicle.pending_command_count == 0
        )
    sim.set_battery_level(61)
    sim.set_software_update_state(4)

    button_id = er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{VIN}_button_force_update")
    await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)
    await settle_until(
        hass,
        lambda: manager.charge_state.get("battery_level") == 61 and manager.software_update_state.get("status") == 4,
    )

    assert state_of(hass, "sensor", f"{VIN}_sensor_battery_level") == "61"


@pytest.mark.parametrize("shutdown", ["unload", "stop"])
async def test_shutdown_disconnects_and_releases_vehicle(hass, sim, monkeypatch, hass_storage, shutdown):
    entry, client = await setup_entry(hass, sim, monkeypatch, hass_storage)
    manager = entry.runtime_data
    manager._next_vcsec_poll = 0
    await settle_until(hass, lambda: manager.connected)
    assert manager.connected
    vehicle_ref = weakref.ref(manager._vehicle)

    if shutdown == "unload":
        assert await hass.config_entries.async_unload(entry.entry_id)
    else:
        await hass.async_stop()
    await hass.async_block_till_done()
    assert not manager.connected
    assert not client.is_connected
    gc.collect()
    assert vehicle_ref() is None


async def test_wedged_link_is_cycled(hass: HomeAssistant, sim, monkeypatch, hass_storage, enable_bluetooth) -> None:
    """If the library resets its session while GATT stays up, cycle the link."""
    entry, client = await setup_entry(hass, sim, monkeypatch, hass_storage)
    manager = entry.runtime_data
    manager._next_vcsec_poll = 0
    await settle_until(hass, lambda: manager.connected)

    # Simulate the library's auth-stall watchdog resetting its own connection.
    manager._vehicle.set_connected(False)
    await settle_until(hass, lambda: not manager.connected)

    assert manager.connected is False
    assert client.is_connected is False


async def test_pairing_issue_when_no_key(hass: HomeAssistant, sim, monkeypatch, hass_storage, enable_bluetooth) -> None:
    monkeypatch.setattr(
        "custom_components.tesla_ble.button.TeslaPairButton._attr_entity_registry_enabled_default", True
    )
    from homeassistant.helpers import issue_registry as ir

    from custom_components.tesla_ble.const import ISSUE_PAIRING_REQUIRED

    entry, _client = await setup_entry(hass, sim, monkeypatch, hass_storage, with_key=False)
    registry = ir.async_get(hass)
    issue_id = f"{ISSUE_PAIRING_REQUIRED}_{entry.entry_id}"
    assert registry.async_get_issue(DOMAIN, issue_id) is not None

    # Pairing clears the issue.
    manager = entry.runtime_data
    manager._next_vcsec_poll = 0
    await settle_until(hass, lambda: manager.connected)
    button_id = er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{VIN}_button_pair")
    await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)
    await settle_until(hass, lambda: manager._vehicle.pending_command_count == 0)

    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_no_pairing_issue_with_key(hass: HomeAssistant, sim, monkeypatch, hass_storage, enable_bluetooth) -> None:
    from homeassistant.helpers import issue_registry as ir

    from custom_components.tesla_ble.const import ISSUE_PAIRING_REQUIRED

    entry, _ = await setup_entry(hass, sim, monkeypatch, hass_storage)
    await settle_until(hass, lambda: bool(entry.runtime_data.charge_state))
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_PAIRING_REQUIRED}_{entry.entry_id}") is None


async def test_setup_pairs_automatically(hass: HomeAssistant, sim, monkeypatch, hass_storage, enable_bluetooth) -> None:
    """The pairing step of onboarding hands off to a single automatic attempt."""
    from homeassistant.components import persistent_notification

    notifications: list[str] = []
    monkeypatch.setattr(
        persistent_notification,
        "async_create",
        lambda hass, message, title=None, notification_id=None: notifications.append(message),
    )

    entry, _client = await setup_entry(hass, sim, monkeypatch, hass_storage, with_key=False, pair_on_setup=True)
    manager = entry.runtime_data
    await settle_until(hass, lambda: manager.paired)

    assert state_of(hass, "sensor", f"{VIN}_sensor_last_command") == "Pair: ok"
    assert any("key card" in message for message in notifications)
    assert any("paired" in message.lower() for message in notifications)


async def test_setup_pairing_reports_unreachable_vehicle(
    hass: HomeAssistant, sim, monkeypatch, hass_storage, enable_bluetooth
) -> None:
    from homeassistant.components import persistent_notification

    notifications: list[str] = []
    monkeypatch.setattr(
        persistent_notification,
        "async_create",
        lambda hass, message, title=None, notification_id=None: notifications.append(message),
    )

    entry, _client = await setup_entry(
        hass, sim, monkeypatch, hass_storage, with_key=False, pair_on_setup=True, reachable=False
    )
    manager = entry.runtime_data
    await settle_until(hass, lambda: any("could not start" in message.lower() for message in notifications))

    assert manager.paired is False
    # The attempt generated the vehicle key, so restarts no longer auto-pair.
    assert manager._vehicle.has_private_key


VIN2 = "5YJ30123456789DEF"
ADDRESS2 = "11:22:33:44:55:66"


async def test_climate_entity(hass: HomeAssistant, sim, monkeypatch, hass_storage, enable_bluetooth) -> None:
    sim.set_inside_temp(21.0)
    sim.set_climate_on(True)
    sim.set_climate_keeper_mode(4)  # Dog Mode
    sim.set_bioweapon_mode(False)

    entry, _client = await setup_entry(hass, sim, monkeypatch, hass_storage)
    manager = entry.runtime_data
    manager._next_vcsec_poll = 0
    await settle_until(hass, lambda: bool(manager.climate_state))

    climate_id = er.async_get(hass).async_get_entity_id("climate", DOMAIN, f"{VIN}_climate_climate")
    assert climate_id, "climate entity not found"
    state = hass.states.get(climate_id)
    assert state.state == "heat_cool"
    assert state.attributes["preset_mode"] == "Dog Mode"
    assert state.attributes["current_temperature"] == 21.0
    assert state.attributes["fan_mode"] == "Normal"

    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": climate_id, "temperature": 22.5}, blocking=True
    )
    await settle_until(hass, lambda: manager._vehicle.pending_command_count == 0)
    assert state_of(hass, "sensor", f"{VIN}_sensor_last_command") == "Set Climate Temp: ok"

    await hass.services.async_call(
        "climate", "set_preset_mode", {"entity_id": climate_id, "preset_mode": "Dog Mode"}, blocking=True
    )
    await settle_until(hass, lambda: manager._vehicle.pending_command_count == 0)
    assert state_of(hass, "sensor", f"{VIN}_sensor_last_command") == "Climate Keeper Dog: ok"

    await hass.services.async_call(
        "climate", "set_fan_mode", {"entity_id": climate_id, "fan_mode": "Bioweapon Mode"}, blocking=True
    )
    await settle_until(hass, lambda: manager._vehicle.pending_command_count == 0)
    assert state_of(hass, "sensor", f"{VIN}_sensor_last_command") == "Bioweapon On: ok"

    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": climate_id, "hvac_mode": "off"}, blocking=True
    )
    await settle_until(hass, lambda: manager._vehicle.pending_command_count == 0)
    assert state_of(hass, "sensor", f"{VIN}_sensor_last_command") == "Climate Off: ok"

    await hass.services.async_call(
        "climate", "set_preset_mode", {"entity_id": climate_id, "preset_mode": "Defrost"}, blocking=True
    )
    await settle_until(hass, lambda: manager._vehicle.pending_command_count == 0)
    results = []
    remove = manager.async_add_listener(lambda: results.append(manager.last_command))
    await hass.services.async_call(
        "climate", "set_preset_mode", {"entity_id": climate_id, "preset_mode": "Normal"}, blocking=True
    )
    await settle_until(hass, lambda: manager._vehicle.pending_command_count == 0)
    remove()
    assert "Defrost Off: ok" in results
    assert manager.last_command == "Climate Keeper Off: ok"


async def test_parity_entities(hass: HomeAssistant, sim, monkeypatch, hass_storage, enable_bluetooth) -> None:
    """ESPhome parity: charging diagnostics, parking brake, sentry, latch."""
    sim.set_charger_pilot_current(16)
    sim.set_charger_phases(3)
    sim.set_charger_voltage(230)
    sim.set_charger_actual_current(10)
    sim.set_sunroof_percent(50)
    sim.set_sentry_mode_state(3)  # Armed
    sim.set_charge_port_latch(3)  # Engaged

    entry, _client = await setup_entry(hass, sim, monkeypatch, hass_storage)
    manager = entry.runtime_data
    manager._next_vcsec_poll = 0
    await settle_until(hass, lambda: manager.charge_state and manager.closures_state)

    power_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{VIN}_sensor_charger_power")
    assert hass.states.get(power_id).attributes["charger_max_current"] == 16
    assert hass.states.get(power_id).attributes["charger_phases"] == 3
    assert state_of(hass, "sensor", f"{VIN}_sensor_charger_power") == "6900"
    assert state_of(hass, "binary_sensor", f"{VIN}_binary_parking_brake") == "on"
    assert state_of(hass, "switch", f"{VIN}_switch_sentry_mode") == "on"
    assert state_of(hass, "lock", f"{VIN}_lock_charge_port_latch") == "locked"

    # Disabled-by-default entities still exist in the registry
    registry = er.async_get(hass)
    await hass.async_block_till_done()
    sunroof_id = registry.async_get_entity_id("cover", DOMAIN, f"{VIN}_cover_sunroof")
    assert hass.states.get(sunroof_id).attributes["current_position"] == 50
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{VIN}_sensor_charge_current_request")
    assert registry.async_get_entity_id("button", DOMAIN, f"{VIN}_button_regenerate_key")


async def test_multiple_vehicles(hass: HomeAssistant, monkeypatch, hass_storage, enable_bluetooth) -> None:
    """Two vehicles coexist: separate connections, devices and entities."""
    sim1 = tesla_ble.SimulatedVehicle(VIN)
    sim1.set_battery_level(55)
    sim2 = tesla_ble.SimulatedVehicle(VIN2)
    sim2.set_battery_level(70)

    clients = {ADDRESS: FakeGattClient(sim1), ADDRESS2: FakeGattClient(sim2)}

    async def establish(_class, device, _name, **kwargs):
        client = clients[device.address]
        client.is_connected = True
        client._on_disconnect = kwargs["disconnected_callback"]
        return client

    from homeassistant.components import bluetooth

    monkeypatch.setattr(
        bluetooth,
        "async_ble_device_from_address",
        lambda _hass, address, **kw: SimpleNamespace(address=address, name="Tesla"),
    )
    monkeypatch.setattr(ble_module, "establish_connection", establish)

    entries = []
    for vin, address in ((VIN, ADDRESS), (VIN2, ADDRESS2)):
        entry = MockConfigEntry(domain=DOMAIN, data={CONF_VIN: vin, CONF_ADDRESS: address}, unique_id=vin)
        key = TEST_PRIVATE_KEY_PEM.encode() + b"\x00"
        hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {
            "version": 1,
            "data": {"private_key": base64.b64encode(key).decode()},
        }
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        entries.append(entry)

    for entry in entries:
        entry.runtime_data._next_vcsec_poll = 0

    await settle_until(
        hass,
        lambda: entries[0].runtime_data.charge_state and entries[1].runtime_data.charge_state,
    )

    assert state_of(hass, "sensor", f"{VIN}_sensor_battery_level") == "55"
    assert state_of(hass, "sensor", f"{VIN2}_sensor_battery_level") == "70"
    assert entries[0].runtime_data.connected
    assert entries[1].runtime_data.connected

    # Each vehicle has its own device in the registry
    from homeassistant.helpers import device_registry as dr

    devices = dr.async_get(hass)
    for entry, vin in ((entries[0], VIN), (entries[1], VIN2)):
        assert devices.async_get_device_by_identifier((DOMAIN, vin), entry.entry_id) is not None


async def test_slow_connect_has_one_tick_and_unload_joins_services(hass, sim, monkeypatch, hass_storage):
    from homeassistant.exceptions import HomeAssistantError

    entry, client = await setup_entry(hass, sim, monkeypatch, hass_storage)
    manager = entry.runtime_data
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def slow_connect(*args, **kwargs):
        nonlocal calls
        calls += 1
        client._on_disconnect = kwargs["disconnected_callback"]
        entered.set()
        await release.wait()
        return client

    monkeypatch.setattr(ble_module, "establish_connection", slow_connect)
    tick = asyncio.create_task(manager._async_tick(None))
    await entered.wait()
    await asyncio.gather(*(manager._async_tick(None) for _ in range(12)))
    service = asyncio.create_task(manager.async_wake())
    await asyncio.sleep(0)
    unload = asyncio.create_task(hass.config_entries.async_unload(entry.entry_id))
    while not manager._stopped:
        await asyncio.sleep(0)
    second_shutdown = asyncio.create_task(manager.async_shutdown())
    await asyncio.sleep(0)
    release.set()
    assert await unload
    await second_shutdown
    await asyncio.gather(tick, service, return_exceptions=True)
    assert tick.cancelled() and service.cancelled()
    assert calls == 1 and not manager.connected and not client.is_connected
    assert manager._vehicle is None and manager._transport._notify_callback is None
    await manager._async_tick(None)
    with pytest.raises(HomeAssistantError, match="unloaded"):
        await manager.async_wake()
    assert calls == 1


async def test_monotonic_wrap_and_stale_telemetry(hass, sim, monkeypatch, hass_storage):
    from homeassistant.components import bluetooth

    from custom_components.tesla_ble import manager as manager_module

    clock = SimpleNamespace(monotonic=lambda: (2**32 + 123_000) / 1000)
    monkeypatch.setattr(manager_module, "time", clock)
    entry, client = await setup_entry(hass, sim, monkeypatch, hass_storage)
    manager = entry.runtime_data
    await settle_until(hass, lambda: bool(manager.charge_state))
    await manager.async_force_update()
    await settle_until(hass, lambda: manager._vehicle.pending_command_count == 0)
    assert manager_module.monotonic_ms() == 123_000
    await client.disconnect()
    monkeypatch.setattr(bluetooth, "async_ble_device_from_address", lambda *a, **kw: None)
    clock.monotonic = lambda: (2**32 + 123_000) / 1000 + 200
    await manager._async_tick(None)
    # The vehicle status is stale, so the car is out of range: everything
    # expires instead of showing day-old values.
    assert state_of(hass, "lock", f"{VIN}_lock_doors") == "unavailable"
    assert state_of(hass, "sensor", f"{VIN}_sensor_battery_level") == "unavailable"
    clock.monotonic = lambda: (2**32 + 123_000) / 1000 + 1400
    await manager._async_tick(None)
    assert state_of(hass, "sensor", f"{VIN}_sensor_battery_level") == "unavailable"
    assert state_of(hass, "binary_sensor", f"{VIN}_binary_connection") == "off"
    pair_id = er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{VIN}_button_pair")
    assert er.async_get(hass).async_get(pair_id).disabled_by == er.RegistryEntryDisabler.INTEGRATION


async def test_asleep_vehicle_keeps_last_known_values(hass, sim, monkeypatch, hass_storage, enable_bluetooth):
    """Infotainment values stay visible while the car sleeps; unreachable expires them."""
    entry, _client = await setup_entry(hass, sim, monkeypatch, hass_storage)
    manager = entry.runtime_data
    await settle_until(hass, lambda: bool(manager.charge_state))
    assert state_of(hass, "sensor", f"{VIN}_sensor_battery_level") == "55"

    # The vehicle falls asleep: infotainment data stops, VCSEC keeps polling.
    manager.vehicle_status = {**manager.vehicle_status, "asleep": True}
    stale = manager._poll_policy.sleep_timeout_ms / 1000 * 2 + 60
    manager._state_received_at["charge_state"] = time.monotonic() - stale
    manager._state_received_at["vehicle_status"] = time.monotonic()
    manager._notify()

    assert manager.state_fresh("charge_state") is False
    assert manager.state_available("charge_state") is True
    assert state_of(hass, "sensor", f"{VIN}_sensor_battery_level") == "55"

    # Once the vehicle status is stale as well it is unreachable: expire.
    manager._state_received_at["vehicle_status"] = time.monotonic() - 3600
    manager._notify()
    assert state_of(hass, "sensor", f"{VIN}_sensor_battery_level") == "unavailable"


async def test_presence_tracker_follows_reachability(hass, sim, monkeypatch, hass_storage, enable_bluetooth):
    """Departure mid-frame cancels the control; return refreshes without replay."""
    entry, client = await setup_entry(hass, sim, monkeypatch, hass_storage)
    manager = entry.runtime_data
    await settle_until(hass, lambda: bool(manager.charge_state) and manager._vehicle.pending_command_count == 0)
    assert state_of(hass, "device_tracker", f"{VIN}_tracker") == "home"
    original_amps = manager.charge_state["charging_amps"]
    key = manager._store.load("private_key")
    await manager.async_set_polling(False)
    write = client.write_gatt_char
    chunks = 0

    async def depart_mid_frame(uuid, chunk, response=False):
        nonlocal chunks
        chunks += 1
        if chunks == 2:
            raise ConnectionError("vehicle left range")
        await write(uuid, chunk, response)

    monkeypatch.setattr(client, "write_gatt_char", depart_mid_frame)
    await manager.async_set_charging_amps(11)
    for _ in range(20):
        await manager._async_tick(None)
        if not manager.connected:
            break
    assert chunks == 2 and not manager.connected
    assert manager._vehicle.pending_command_count == 0
    assert "outcome unknown" in manager.last_command

    # The car leaves: no status, no adverts.
    manager._state_received_at["vehicle_status"] = time.monotonic() - 3600
    manager.rssi = None
    manager._notify()
    assert state_of(hass, "device_tracker", f"{VIN}_tracker") == "not_home"

    returned_client = FakeGattClient(sim)

    async def reconnect(_class, _device, _name, **kwargs):
        returned_client._on_disconnect = kwargs["disconnected_callback"]
        return returned_client

    monkeypatch.setattr(ble_module, "establish_connection", reconnect)
    await manager.async_set_polling(True)
    sim.set_battery_level(51)
    await manager.async_force_update()
    await settle_until(
        hass, lambda: manager.charge_state.get("battery_level") == 51 and manager._vehicle.pending_command_count == 0
    )
    assert state_of(hass, "device_tracker", f"{VIN}_tracker") == "home"
    assert manager.charge_state["charging_amps"] == original_amps
    assert manager._store.load("private_key") == key and manager.paired


async def test_number_limits_follow_vehicle(hass, sim, monkeypatch, hass_storage, enable_bluetooth):
    """Both number ranges come from the vehicle, not from a config option."""
    entry, _client = await setup_entry(hass, sim, monkeypatch, hass_storage)
    manager = entry.runtime_data
    await settle_until(hass, lambda: bool(manager.charge_state))

    manager.charge_state = {
        **manager.charge_state,
        "charge_limit_soc": 80,
        "charge_limit_soc_min": 50,
        "charge_limit_soc_max": 90,
        "charge_current_request_max": 16,
        "charging_amps": 12,
    }
    manager._notify()

    limit = hass.states.get("number.tesla_789abc_charge_limit")
    amps = hass.states.get("number.tesla_789abc_charging_amps")
    assert limit.state == "80"
    assert limit.attributes["min"] == 50
    assert limit.attributes["max"] == 90
    assert amps.state == "12"
    assert amps.attributes["max"] == 16


async def test_fleet_style_entities_and_attributes(hass, sim, monkeypatch, hass_storage, enable_bluetooth):
    """Aggregate sensors and grouped attributes mirror the fleet integration."""
    sim.set_charger_voltage(230)
    sim.set_charger_actual_current(10)
    sim.set_charger_phases(3)
    sim.set_scheduled_charging_mode(1)
    sim.set_door_open("driver_front", True)

    entry, _client = await setup_entry(hass, sim, monkeypatch, hass_storage)
    manager = entry.runtime_data
    manager._next_vcsec_poll = 0
    await settle_until(hass, lambda: bool(manager.charge_state) and bool(manager.closures_state))

    doors = hass.states.get("binary_sensor.tesla_789abc_doors")
    assert doors.state == "on"
    assert doors.attributes["Driver Front"] == "Open"
    assert doors.attributes["Driver Rear"] == "Closed"

    assert state_of(hass, "binary_sensor", f"{VIN}_binary_scheduled_charging") == "on"

    power = hass.states.get(er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{VIN}_sensor_charger_power"))
    assert power.attributes["charger_volts"] == 230
    assert power.attributes["charger_phases"] == 3
    assert power.attributes["charger_amps_actual"] == 10

    manager._on_vehicle_status({"closures_raw": {"front_driver_door": 0}})
    doors = hass.states.get("binary_sensor.tesla_789abc_doors")
    assert doors.state == "unknown"
    assert doors.attributes["Driver Rear"] == "Unknown"
    manager._on_closures_state({"window_open_driver_front": False})
    windows_id = er.async_get(hass).async_get_entity_id("cover", DOMAIN, f"{VIN}_cover_windows")
    windows = hass.states.get(windows_id)
    assert windows.state == "unknown"
    assert windows.attributes["Driver Rear"] == "Unknown"
    manager._on_closures_state({"window_open_driver_front": True})
    assert hass.states.get(windows_id).state == "open"


async def test_software_update_entity(hass, sim, monkeypatch, hass_storage, enable_bluetooth):
    """The update entity reports availability without inventing firmware versions."""
    sim.set_software_update_state(4)  # Available
    entry, _client = await setup_entry(hass, sim, monkeypatch, hass_storage)
    manager = entry.runtime_data
    await manager.async_force_update()
    await settle_until(hass, lambda: bool(manager.software_update_state))
    update_id = er.async_get(hass).async_get_entity_id("update", DOMAIN, f"{VIN}_update_software_update")
    update = hass.states.get(update_id)
    assert update.state == "on"
    assert update.attributes["installed_version"] == "Unknown"
    assert update.attributes["status"] == "Available"

    # No update pending (tag 1) reads as up to date, like the Fleet integration.
    sim.set_software_update_state(1)
    await manager.async_force_update()
    await settle_until(hass, lambda: manager.software_update_state.get("status") == 1)
    assert hass.states.get(update_id).state == "off"
    assert hass.states.get(update_id).attributes["status"] == "Up to date"

    sim.set_software_update_state(0)
    await manager.async_force_update()
    await settle_until(hass, lambda: manager.software_update_state.get("status") == 0)
    assert hass.states.get(update_id).state == "unknown"
    assert hass.states.get(update_id).attributes["status"] == "Unknown"

    for status, field in ((2, "install_perc"), (5, "download_perc"), (6, "download_perc")):
        manager._on_software_update_state({"status": status, "version": "2026.40.1", field: 42.5})
        update = hass.states.get(update_id)
        assert update.state == "on"
        assert update.attributes["in_progress"] is True
        assert update.attributes["update_percentage"] == 42.5
        assert update.attributes["supported_features"] == 4


async def test_entity_cleanup_and_defaults_preserve_existing_power_entity(hass, sim, monkeypatch, hass_storage):
    from homeassistant.helpers.icon import async_get_icons

    entry, _ = await setup_entry(hass, sim, monkeypatch, hass_storage)
    registry = er.async_get(hass)
    power_id = registry.async_get_entity_id("sensor", DOMAIN, f"{VIN}_sensor_charger_power")
    registry.async_update_entity(power_id, name="My charging power")
    registry.async_update_entity_options(power_id, "sensor", {"unit_of_measurement": "kW"})
    retired = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{VIN}_sensor_charger_power_estimated",
        config_entry=entry,
        suggested_object_id="old_power_estimate",
    )
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert registry.async_get(retired.entity_id) is None
    assert registry.async_get(power_id).name == "My charging power"
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{VIN}_sensor_charger_power") == power_id
    enabled = [s for s in hass.states.async_all("sensor") if s.attributes.get("device_class") in ("power", "energy")]
    assert sorted(s.attributes["device_class"] for s in enabled) == ["energy", "energy", "power"]
    for key in ("button_pair", "button_regenerate_key"):
        entity_id = registry.async_get_entity_id("button", DOMAIN, f"{VIN}_{key}")
        assert registry.async_get(entity_id).disabled_by == er.RegistryEntryDisabler.INTEGRATION
        assert registry.async_get(entity_id).entity_category == EntityCategory.DIAGNOSTIC
    for key in (
        "evse_max_current",
        "vehicle_max_charge_current",
        "charge_current_request",
        "charger_phases",
        "tpms_front_left",
        "tpms_front_right",
        "tpms_rear_left",
        "tpms_rear_right",
    ):
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{VIN}_sensor_{key}")
        assert registry.async_get(entity_id).disabled_by is None
        assert registry.async_get(entity_id).entity_category is None
        assert hass.states.get(entity_id) is not None
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{VIN}_sensor_charge_limit_soc") is None
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{VIN}_sensor_software_update") is None
    for key in ("door_driver_front", "window_driver_front"):
        assert registry.async_get_entity_id("binary_sensor", DOMAIN, f"{VIN}_binary_{key}") is None
    assert hass.states.get(power_id).attributes["unit_of_measurement"] == "W"
    for state in hass.states.async_all("sensor"):
        assert state.attributes.get("icon")
    tracker_id = registry.async_get_entity_id("device_tracker", DOMAIN, f"{VIN}_tracker")
    assert hass.states.get(tracker_id).attributes["icon"] == "mdi:car-electric"
    assert hass.states.get(tracker_id).attributes["friendly_name"].endswith(" Location")
    for key in ("iec61851_state", "charge_limit_reason", "charger_voltage", "charger_actual_current"):
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{VIN}_sensor_{key}")
        assert registry.async_get(entity_id).disabled_by is None
        assert registry.async_get(entity_id).entity_category is None
        assert hass.states.get(entity_id) is not None
    for domain, key, name in (("switch", "polling", "Polling"), ("update", "software_update", "Firmware")):
        entity_id = registry.async_get_entity_id(domain, DOMAIN, f"{VIN}_{domain}_{key}")
        assert hass.states.get(entity_id).attributes["friendly_name"].endswith(f" {name}")
    icons = (await async_get_icons(hass, "entity", [DOMAIN]))[DOMAIN]
    assert icons["button"]["wake"]["default"] == "mdi:sleep-off"
    assert icons["lock"]["doors"]["state"]["unlocked"] == "mdi:car-door-lock-open"


async def test_energy_accumulates_observed_sessions_and_survives_reload(hass, sim, monkeypatch, hass_storage):
    entry, _ = await setup_entry(hass, sim, monkeypatch, hass_storage)
    manager = entry.runtime_data
    await manager.async_set_polling(False)
    energy_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{VIN}_sensor_charge_energy_added")
    manager._on_charge_state({"charge_energy_added": 7.5})
    assert float(hass.states.get(energy_id).state) == 0
    started = hass.states.get(energy_id).attributes["tracking_since"]
    for value in (8.5, 8.5, 8.48, 8.5, float("nan"), -1, None):
        manager._on_charge_state({"charge_energy_added": value})
    assert float(hass.states.get(energy_id).state) == 1
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    manager = entry.runtime_data
    assert float(hass.states.get(energy_id).state) == 1
    assert hass.states.get(energy_id).attributes["tracking_since"] == started
    for value in (9.5, 0, 2.25, 2.25):
        manager._on_charge_state({"charge_energy_added": value})
    assert float(hass.states.get(energy_id).state) == 4.25
    assert hass.states.get(energy_id).attributes["session_energy_kwh"] == 2.25
    manager._on_charge_state({"charger_voltage": 231, "charger_actual_current": 8, "charger_phases": 3})
    power_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{VIN}_sensor_charger_power")
    assert float(hass.states.get(power_id).state) == 5544
    manager._on_charge_state({"charger_power": 5})
    assert float(hass.states.get(power_id).state) == 5000


async def test_energy_meter_revisions_are_not_new_sessions(hass, sim, monkeypatch, hass_storage):
    """The car revised this ended session downward (2026-10-12 data); none of it is new energy."""
    entry, _ = await setup_entry(hass, sim, monkeypatch, hass_storage)
    manager = entry.runtime_data
    await manager.async_set_polling(False)
    for value in (27.18, 27.10, 27.08, 27.04, 26.72, 26.70, 26.62):
        manager._on_charge_state({"charge_energy_added": value})
    assert manager.energy_total == 0
    for value in (0.05, 0.4, 1.2):
        manager._on_charge_state({"charge_energy_added": value})
    assert manager.energy_total == 1.2


async def test_ac_energy_integrates_power_and_survives_reload(hass, sim, monkeypatch, hass_storage):
    """AC energy integrates power samples; gaps where charging may have stopped are capped."""
    entry, _ = await setup_entry(hass, sim, monkeypatch, hass_storage)
    manager = entry.runtime_data
    await manager.async_set_polling(False)
    monkeypatch.setattr(manager, "disconnect_delay", 10**9)
    clock = SimpleNamespace(value=1_000_000.0)
    monkeypatch.setattr(manager_module, "time", SimpleNamespace(monotonic=lambda: clock.value))
    manager.ac_energy_total = 0.0
    manager._ac_energy_last_power = None
    manager._ac_energy_last_time = 0.0
    manager._ac_energy_charging = False
    energy_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{VIN}_sensor_charge_energy")
    charging = {"charger_voltage": 230, "charger_actual_current": 10, "charger_phases": 1, "charging_state": "Charging"}
    manager._on_charge_state(charging)
    assert float(hass.states.get(energy_id).state) == 0
    clock.value += 600
    manager._on_charge_state(charging)
    assert float(hass.states.get(energy_id).state) == pytest.approx(0.3833, abs=0.001)
    clock.value += 3600
    manager._on_charge_state(dict(charging, charging_state="Complete"))
    assert float(hass.states.get(energy_id).state) == pytest.approx(0.3833 + 2.3 * 300 / 3600, abs=0.001)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert float(hass.states.get(energy_id).state) == pytest.approx(0.575, abs=0.01)


async def test_automatic_poll_does_not_wake_car_with_stale_awake_telemetry(hass, sim, monkeypatch, hass_storage):
    entry, _ = await setup_entry(hass, sim, monkeypatch, hass_storage)
    manager = entry.runtime_data
    await settle_until(hass, lambda: bool(manager.charge_state) and manager._vehicle.pending_command_count == 0)
    await manager.async_set_polling(False)
    sim.set_sleeping(True)
    manager._vehicle.vcsec_poll()
    await manager._transport.async_flush()
    await settle_until(
        hass, lambda: manager.vehicle_status.get("asleep") and manager._vehicle.pending_command_count == 0
    )
    manager.vehicle_status = dict(manager.vehicle_status, asleep=False)
    manager._poll_policy.set_active_interval_ms(0)
    manager._poll_policy.set_awake_interval_ms(0)
    manager._next_vcsec_poll = time.monotonic() + 3600
    manager.polling = True
    await manager._async_handle_polls()
    manager.polling = False
    await settle_until(hass, lambda: manager._vehicle.pending_command_count == 0)
    manager._vehicle.vcsec_poll()
    await manager._transport.async_flush()
    await settle_until(hass, lambda: manager._vehicle.pending_command_count == 0)
    assert manager.vehicle_status["asleep"] is True


async def test_charge_completion_time_is_anchored_to_telemetry(hass, sim, monkeypatch, hass_storage):
    entry, _ = await setup_entry(hass, sim, monkeypatch, hass_storage)
    manager = entry.runtime_data
    await settle_until(hass, lambda: bool(manager.charge_state))
    state = dict(manager.charge_state, minutes_to_full_charge=60)
    manager._on_charge_state(state)
    initial = state_of(hass, "sensor", f"{VIN}_sensor_minutes_to_full_charge")
    assert initial == (manager.charge_state_received_at + timedelta(minutes=60)).replace(microsecond=0).isoformat()
    later = dt_util.utcnow() + timedelta(minutes=10)
    with monkeypatch.context() as patches:
        patches.setattr(dt_util, "utcnow", lambda: later)
        manager._notify()
        assert state_of(hass, "sensor", f"{VIN}_sensor_minutes_to_full_charge") == initial
        manager._on_charge_state(state)
        assert (
            state_of(hass, "sensor", f"{VIN}_sensor_minutes_to_full_charge")
            == (later + timedelta(minutes=60)).replace(microsecond=0).isoformat()
        )
    manager._on_charge_state(dict(state, minutes_to_full_charge=0))
    assert state_of(hass, "sensor", f"{VIN}_sensor_minutes_to_full_charge") == "unknown"


async def test_optional_controls_detect_capabilities_and_follow_reported_state(hass, sim, monkeypatch, hass_storage):
    entry, _ = await setup_entry(hass, sim, monkeypatch, hass_storage)
    manager = entry.runtime_data
    registry = er.async_get(hass)
    await settle_until(hass, lambda: bool(manager.climate_state) and manager._vehicle.pending_command_count == 0)
    assert registry.async_get_entity_id("cover", DOMAIN, f"{VIN}_cover_sunroof") is None
    assert registry.async_get_entity_id("switch", DOMAIN, f"{VIN}_switch_steering_wheel_heat") is None
    assert not hass.states.async_all("select")
    sim.set_seat_heater(tesla_ble.SeatPosition.FRONT_LEFT, 0)
    sim.set_seat_cooler(tesla_ble.SeatPosition.FRONT_LEFT, 0)
    sim.set_auto_seat_climate(tesla_ble.SeatPosition.FRONT_LEFT, False)
    sim.set_steering_wheel_heat(False)
    sim.set_cabin_overheat_protection(0)
    sim.set_cabin_overheat_protection_temp(1)
    sim.set_sunroof_percent(0)
    await manager.async_force_update()
    await settle_until(
        hass, lambda: "seat_heater_left" in manager.climate_state and manager._vehicle.pending_command_count == 0
    )
    await hass.async_block_till_done()

    heat_id = registry.async_get_entity_id("select", DOMAIN, f"{VIN}_select_seat_climate_left")
    roof_id = registry.async_get_entity_id("cover", DOMAIN, f"{VIN}_cover_sunroof")
    assert state_of(hass, "select", f"{VIN}_select_seat_climate_left") == "Off"
    assert registry.async_get_entity_id("select", DOMAIN, f"{VIN}_select_seat_climate_right") is None
    assert registry.async_get_entity_id("select", DOMAIN, f"{VIN}_select_seat_fan_front_left") is None
    assert registry.async_get_entity_id("switch", DOMAIN, f"{VIN}_switch_auto_seat_climate_left") is None
    assert hass.states.get(heat_id).attributes["options"] == [
        "Off",
        "Low heat",
        "Medium heat",
        "High heat",
        "Low cool",
        "Medium cool",
        "High cool",
        "Auto",
    ]
    assert hass.states.get(heat_id).attributes["icon"] == "mdi:car-seat"
    await hass.services.async_call(
        "select", "select_option", {"entity_id": heat_id, "option": "High heat"}, blocking=True
    )
    await hass.services.async_call("select", "select_option", {"entity_id": heat_id, "option": "Off"}, blocking=True)
    await settle_until(hass, lambda: manager._vehicle.pending_command_count == 0)
    assert manager.climate_state["seat_heater_left"] == 0
    await hass.services.async_call("select", "select_option", {"entity_id": heat_id, "option": "Auto"}, blocking=True)
    await settle_until(hass, lambda: hass.states.get(heat_id).state == "Auto")
    assert manager.climate_state["auto_seat_climate_left"] is True
    await hass.services.async_call(
        "select", "select_option", {"entity_id": heat_id, "option": "High heat"}, blocking=True
    )
    await settle_until(hass, lambda: hass.states.get(heat_id).state == "High heat")
    assert manager.climate_state["auto_seat_climate_left"] is False
    assert hass.states.get(heat_id).attributes["icon"] == "mdi:car-seat-heater"
    await hass.services.async_call(
        "select", "select_option", {"entity_id": heat_id, "option": "Medium cool"}, blocking=True
    )
    await settle_until(hass, lambda: hass.states.get(heat_id).state == "Medium cool")
    assert manager.climate_state["seat_heater_left"] == 0
    assert hass.states.get(heat_id).attributes["icon"] == "mdi:car-seat-cooler"
    await hass.services.async_call("select", "select_option", {"entity_id": heat_id, "option": "Off"}, blocking=True)
    await settle_until(hass, lambda: hass.states.get(heat_id).state == "Off")
    assert manager.climate_state["seat_fan_front_left"] == 0
    cop_id = registry.async_get_entity_id("select", DOMAIN, f"{VIN}_select_cabin_overheat_protection")
    assert hass.states.get(cop_id).attributes["options"] == ["Off", "On"]
    await hass.services.async_call("select", "select_option", {"entity_id": cop_id, "option": "On"}, blocking=True)
    await settle_until(hass, lambda: hass.states.get(cop_id).state == "On")
    temp_id = registry.async_get_entity_id("select", DOMAIN, f"{VIN}_select_cop_activation_temperature")
    await hass.services.async_call("select", "select_option", {"entity_id": temp_id, "option": "High"}, blocking=True)
    await settle_until(hass, lambda: hass.states.get(temp_id).state == "High")
    await hass.services.async_call("cover", "set_cover_position", {"entity_id": roof_id, "position": 42}, blocking=True)
    await settle_until(hass, lambda: hass.states.get(roof_id).attributes.get("current_position") == 42)
    await manager.async_set_polling(False)
    remembered = set(manager.capabilities)
    manager._on_climate_state({"inside_temp_celsius": 22})
    assert registry.async_get(heat_id) is not None
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert remembered <= entry.runtime_data.capabilities
    assert registry.async_get_entity_id("select", DOMAIN, f"{VIN}_select_seat_climate_left") == heat_id
    assert hass.states.get(heat_id).state == "unknown"


def test_software_update_release_url():
    """A known update version links to its release announcement."""
    manager = SimpleNamespace(
        vin=VIN,
        device_name="Tesla",
        address=ADDRESS,
        software_update_state={"status": 4, "version": "2026.40.1 abc123"},
    )
    entity = TeslaFirmwareUpdate(manager)
    assert entity.latest_version == "2026.40.1 abc123"
    assert entity.release_url == ("https://www.notateslaapp.com/software-updates/version/2026.40.1/release-notes")
    manager.software_update_state = {}
    assert entity.release_url is None


async def test_software_update_install_and_cancel(hass, sim, monkeypatch, hass_storage, enable_bluetooth):
    """One update entity owns installation and cancellation, with state guards."""
    sim.set_software_update_state(4)  # Available
    entry, _client = await setup_entry(hass, sim, monkeypatch, hass_storage)
    manager = entry.runtime_data
    await manager.async_force_update()
    await settle_until(hass, lambda: manager.software_update_state.get("status") == 4)

    registry = er.async_get(hass)
    update_id = registry.async_get_entity_id("update", DOMAIN, f"{VIN}_update_software_update")
    assert update_id
    assert hass.states.get(update_id).attributes["can_cancel"] is False
    with pytest.raises(HomeAssistantError, match="cancellable"):
        await hass.services.async_call(DOMAIN, "cancel_software_update", {"entity_id": update_id}, blocking=True)

    await hass.services.async_call("update", "install", {"entity_id": update_id}, blocking=True)
    await settle_until(hass, lambda: manager.software_update_state.get("status") == 3)
    assert hass.states.get(update_id).attributes["status"] == "Scheduled"
    assert hass.states.get(update_id).attributes["can_cancel"] is True

    await hass.services.async_call(DOMAIN, "cancel_software_update", {"entity_id": update_id}, blocking=True)
    await settle_until(hass, lambda: manager.software_update_state.get("status") == 4)
    assert hass.states.get(update_id).attributes["status"] == "Available"
    manager._state_received_at["software_update_state"] = time.monotonic() - 86400
    with pytest.raises(HomeAssistantError, match="freshly reported"):
        await manager.async_install_software_update()


async def test_polling_switch_pauses_and_persists(hass, sim, monkeypatch, hass_storage, enable_bluetooth):
    """The polling switch pauses polling, keeps values and survives a reload."""
    entry, _client = await setup_entry(hass, sim, monkeypatch, hass_storage)
    manager = entry.runtime_data
    await settle_until(hass, lambda: bool(manager.charge_state) and manager._vehicle.pending_command_count == 0)

    switch_id = er.async_get(hass).async_get_entity_id("switch", DOMAIN, f"{VIN}_switch_polling")
    assert switch_id
    await hass.services.async_call("switch", "turn_off", {"entity_id": switch_id}, blocking=True)
    assert manager.polling is False
    assert manager._store.load("polling") == b"0"

    await settle(hass, 3)
    assert manager._vehicle.pending_command_count == 0, "paused polling must not enqueue commands"
    assert state_of(hass, "sensor", f"{VIN}_sensor_battery_level") == "55", "paused polling keeps last values"

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.polling is False

    switch_id = er.async_get(hass).async_get_entity_id("switch", DOMAIN, f"{VIN}_switch_polling")
    await hass.services.async_call("switch", "turn_on", {"entity_id": switch_id}, blocking=True)
    assert entry.runtime_data.polling is True
    assert entry.runtime_data._store.load("polling") == b"1"


async def test_periodic_polls_do_not_fill_queue_behind_existing_work(hass, sim, monkeypatch, hass_storage):
    from custom_components.tesla_ble import manager as manager_module

    entry, _ = await setup_entry(hass, sim, monkeypatch, hass_storage)
    manager = entry.runtime_data
    await manager.async_force_update()
    pending = manager._vehicle.pending_command_count
    assert pending > 0
    monkeypatch.setattr(manager_module, "time", SimpleNamespace(monotonic=lambda: 1_000_000.0))
    manager._next_vcsec_poll = 0
    await manager._async_tick(None)
    assert manager._vehicle.pending_command_count <= pending


@pytest.mark.parametrize("write_fails", [False, True])
async def test_pair_waits_for_durable_key_and_aborts_on_disk_error(hass, sim, monkeypatch, hass_storage, write_fails):
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers.storage import Store
    from homeassistant.util.file import WriteError

    entry, client = await setup_entry(hass, sim, monkeypatch, hass_storage, with_key=False)
    manager = entry.runtime_data
    entered, release = asyncio.Event(), asyncio.Event()
    original = Store._async_write_data

    async def blocked_write(store, data):
        entered.set()
        await release.wait()
        if write_fails:
            raise WriteError("disk full")
        await original(store, data)

    with monkeypatch.context() as patches:
        patches.setattr(Store, "_async_write_data", blocked_write)
        pair = asyncio.create_task(manager.async_pair())
        await entered.wait()
        assert not manager.connected and not client.frames
        release.set()
        if write_fails:
            with pytest.raises(HomeAssistantError, match="persist"):
                await pair
            assert not manager.connected and not client.frames
        else:
            await pair
            assert manager.connected
            assert "private_key" in hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_failed_pair_and_regenerated_key_stay_unpaired_on_reload(hass, sim, monkeypatch, hass_storage):
    from homeassistant.helpers import issue_registry as ir

    from custom_components.tesla_ble.const import ISSUE_PAIRING_REQUIRED

    entry, client = await setup_entry(hass, sim, monkeypatch, hass_storage, with_key=False)
    manager = entry.runtime_data
    await manager.async_pair()
    await manager._async_disconnect()
    assert manager.last_command.startswith("Pair: failed") and not manager.paired
    key = manager._store.load("private_key")
    assert await hass.config_entries.async_reload(entry.entry_id)
    manager = entry.runtime_data
    assert manager._store.load("private_key") == key and not manager.paired
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_PAIRING_REQUIRED}_{entry.entry_id}")
    await manager.async_regenerate_key()
    assert manager._store.load("private_key") != key and not manager.paired
    assert await hass.config_entries.async_reload(entry.entry_id)
    assert not entry.runtime_data.paired


async def test_regeneration_does_not_acknowledge_a_failed_save(hass, sim, monkeypatch, hass_storage):
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers.storage import Store
    from homeassistant.util.file import WriteError

    entry, _ = await setup_entry(hass, sim, monkeypatch, hass_storage)
    manager = entry.runtime_data

    async def fail_write(*args):
        raise WriteError("disk full")

    with monkeypatch.context() as patches:
        patches.setattr(Store, "_async_write_data", fail_write)
        with pytest.raises(HomeAssistantError, match="persist"):
            await manager.async_regenerate_key()
    assert manager.last_command == "Regenerate Key: failed (credentials not persisted)"
    assert not manager.paired
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_pairing_issues_are_entry_scoped_and_removal_deletes_credentials(hass, sim, monkeypatch, hass_storage):
    from homeassistant.helpers import issue_registry as ir

    from custom_components.tesla_ble.const import ISSUE_PAIRING_REQUIRED

    first, _ = await setup_entry(hass, sim, monkeypatch, hass_storage, with_key=False)
    second = MockConfigEntry(domain=DOMAIN, data={CONF_VIN: VIN2, CONF_ADDRESS: ADDRESS2}, unique_id=VIN2)
    key = TEST_PRIVATE_KEY_PEM.encode() + b"\0"
    hass_storage[f"{DOMAIN}.{second.entry_id}"] = {
        "version": 1,
        "data": {
            "private_key": base64.b64encode(key).decode(),
            "paired_key": base64.b64encode(hashlib.sha256(key).hexdigest().encode()).decode(),
        },
    }
    second.add_to_hass(hass)
    assert await hass.config_entries.async_setup(second.entry_id)
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, f"{ISSUE_PAIRING_REQUIRED}_{first.entry_id}")
    assert registry.async_get_issue(DOMAIN, f"{ISSUE_PAIRING_REQUIRED}_{second.entry_id}") is None
    assert await hass.config_entries.async_remove(second.entry_id)
    assert f"{DOMAIN}.{second.entry_id}" not in hass_storage
    assert registry.async_get_issue(DOMAIN, f"{ISSUE_PAIRING_REQUIRED}_{first.entry_id}")


async def test_diagnostics_redact_identifiers(hass, sim, monkeypatch, hass_storage):
    from custom_components.tesla_ble.diagnostics import async_get_config_entry_diagnostics

    entry, _ = await setup_entry(hass, sim, monkeypatch, hass_storage)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["entry"]["data"]["vin"] == "**REDACTED**"
    assert diagnostics["entry"]["data"]["address"] == "**REDACTED**"
    assert diagnostics["connection"]["address"] == "**REDACTED**"
