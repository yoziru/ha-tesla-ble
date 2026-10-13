"""Role permissions at HA entity and command boundaries.

The simulated car accepts controls regardless of key role, so these checks
exercise the integration's policy, not Tesla firmware's permission enforcement.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import tesla_ble
from homeassistant.components.update import UpdateEntityFeature
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from test_manager import VIN, settle, settle_until, setup_entry, state_of

from custom_components.tesla_ble.const import DOMAIN
from custom_components.tesla_ble.diagnostics import async_get_config_entry_diagnostics


@pytest.mark.parametrize("role", [None, "DRIVER", "CHARGING_MANAGER", "VEHICLE_MONITOR"])
async def test_role_entities_keep_telemetry_and_only_allowed_controls(hass, sim, monkeypatch, hass_storage, role):
    sim.set_seat_heater(tesla_ble.SeatPosition.FRONT_LEFT, 1)
    sim.set_steering_wheel_heat(True)
    sim.set_sunroof_percent(20)
    sim.set_software_update_state(4)
    entry, _ = await setup_entry(hass, sim, monkeypatch, hass_storage, role=role)
    registry = er.async_get(hass)
    full_control = role in (None, "DRIVER")
    charging_control = role != "VEHICLE_MONITOR"

    # Reload also exercises remembered capabilities, which must not resurrect
    # controls for restricted keys.
    for reload in (False, True):
        if reload:
            await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done(wait_background_tasks=True)
        sim.set_locked(not reload)
        sim.set_charge_port_door_open(reload)
        sim.set_charge_port_latch(2 if reload else 3)  # Disengaged / engaged
        refresh_id = registry.async_get_entity_id("button", DOMAIN, f"{VIN}_button_force_update")
        update_id = registry.async_get_entity_id("update", DOMAIN, f"{VIN}_update_software_update")
        await hass.services.async_call("button", "press", {"entity_id": refresh_id}, blocking=True)
        await settle_until(hass, lambda update_id=update_id: hass.states.get(update_id).state == "on")
        await hass.async_block_till_done()
        assert float(state_of(hass, "sensor", f"{VIN}_sensor_battery_level")) == 55
        assert float(state_of(hass, "sensor", f"{VIN}_sensor_outside_temp")) == 11
        assert float(state_of(hass, "sensor", f"{VIN}_sensor_odometer")) > 0
        assert registry.async_get_entity_id("device_tracker", DOMAIN, f"{VIN}_tracker") is not None

        entities = er.async_entries_for_config_entry(registry, entry.entry_id)
        for domain in ("lock", "cover", "climate", "select"):
            assert any(e.domain == domain for e in entities) == full_control
        for domain, key, allowed in (
            ("switch", "switch_charging", charging_control),
            ("number", "number_charge_limit", charging_control),
            ("number", "number_charge_amps", charging_control),
            ("button", "button_wake", charging_control),
            ("button", "button_honk_horn", full_control),
            ("button", "button_flash_lights", full_control),
            ("switch", "switch_sentry_mode", full_control),
            ("switch", "switch_steering_wheel_heat", full_control),
        ):
            assert bool(registry.async_get_entity_id(domain, DOMAIN, f"{VIN}_{key}")) == allowed
        assert registry.async_get_entity_id("button", DOMAIN, f"{VIN}_button_pair") is not None
        assert registry.async_get_entity_id("switch", DOMAIN, f"{VIN}_switch_polling") is not None
        update = hass.states.get(update_id)
        assert update.state == "on"
        assert bool(update.attributes["supported_features"] & UpdateEntityFeature.INSTALL) == full_control

        if not full_control:
            for key in ("charge_port", "doors_lock", "charge_port_latch"):
                assert state_of(hass, "binary_sensor", f"{VIN}_binary_{key}") == ("on" if reload else "off")
        if not charging_control:
            assert float(state_of(hass, "sensor", f"{VIN}_sensor_charge_limit")) == 80
            assert float(state_of(hass, "sensor", f"{VIN}_sensor_charging_amps")) == 16

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["configured_key_role"] == (role or "DRIVER")
    assert (
        state_of(hass, "sensor", f"{VIN}_sensor_key_access")
        == {
            None: "Vehicle controls",
            "DRIVER": "Vehicle controls",
            "CHARGING_MANAGER": "Charging controls",
            "VEHICLE_MONITOR": "Read-only monitoring",
        }[role]
    )


@pytest.mark.parametrize("role", ["CHARGING_MANAGER", "VEHICLE_MONITOR"])
async def test_restricted_commands_are_rejected_before_any_ble_work(hass, sim, monkeypatch, hass_storage, role):
    sim.set_software_update_state(4)
    entry, client = await setup_entry(hass, sim, monkeypatch, hass_storage, role=role)
    manager = entry.runtime_data
    await manager.async_set_polling(False)
    await manager.async_force_update()
    await settle_until(
        hass, lambda: bool(manager.software_update_state) and manager._vehicle.pending_command_count == 0
    )
    frames = list(client.frames)
    for command in (
        lambda: manager.async_lock_doors(False),
        lambda: manager.async_set_climate(True),
        manager.async_open_charge_port,
        manager.async_close_charge_port,
        manager.async_unlock_charge_port,
        manager.async_install_software_update,
    ):
        with pytest.raises(HomeAssistantError, match="not allowed"):
            await command()
    if role == "VEHICLE_MONITOR":
        for command in (
            manager.async_wake,
            lambda: manager.async_set_charging(True),
            lambda: manager.async_set_charging_amps(12),
            lambda: manager.async_set_charging_limit(70),
        ):
            with pytest.raises(HomeAssistantError, match="not allowed"):
                await command()
    assert client.frames == frames
    await settle(hass)
    assert client.frames == frames  # No deferred transmission either.

    sim.set_software_update_state(5)  # Downloading: cancellation is otherwise valid.
    await manager.async_force_update()
    await settle_until(
        hass, lambda: manager.software_update_state.get("status") == 5 and manager._vehicle.pending_command_count == 0
    )
    frames = list(client.frames)
    with pytest.raises(HomeAssistantError, match="not allowed"):
        await manager.async_cancel_software_update()
    await settle(hass)
    assert client.frames == frames


async def test_charging_manager_controls_report_vehicle_results(hass, sim, monkeypatch, hass_storage):
    entry, _ = await setup_entry(hass, sim, monkeypatch, hass_storage, role="CHARGING_MANAGER")
    registry = er.async_get(hass)
    await settle_until(hass, lambda: state_of(hass, "switch", f"{VIN}_switch_charging") == "on")
    for domain, service, key, extra, expected in (
        ("switch", "turn_off", "switch_charging", {}, "off"),
        ("switch", "turn_on", "switch_charging", {}, "on"),
        ("number", "set_value", "number_charge_amps", {"value": 12}, "12"),
        ("number", "set_value", "number_charge_limit", {"value": 70}, "70"),
    ):
        entity_id = registry.async_get_entity_id(domain, DOMAIN, f"{VIN}_{key}")
        await hass.services.async_call(domain, service, {"entity_id": entity_id, **extra}, blocking=True)
        await settle_until(
            hass, lambda entity_id=entity_id, expected=expected: hass.states.get(entity_id).state == expected
        )

    await entry.runtime_data.async_set_polling(False)
    sim.set_sleeping(True)
    await entry.runtime_data.async_set_polling(True)
    await settle_until(hass, lambda: state_of(hass, "binary_sensor", f"{VIN}_binary_asleep") == "on")
    wake_id = registry.async_get_entity_id("button", DOMAIN, f"{VIN}_button_wake")
    await hass.services.async_call("button", "press", {"entity_id": wake_id}, blocking=True)
    await settle_until(hass, lambda: state_of(hass, "binary_sensor", f"{VIN}_binary_asleep") == "off")


async def test_command_access_is_independent_of_display_name(hass, sim, monkeypatch, hass_storage):
    entry, client = await setup_entry(hass, sim, monkeypatch, hass_storage, role="CHARGING_MANAGER")
    manager = entry.runtime_data
    await manager.async_set_polling(False)
    frames = list(client.frames)
    # A familiar charging label must not grant access to an unclassified action.
    with pytest.raises(HomeAssistantError, match="not allowed"):
        await manager.async_command("Set Charging Amps", lambda cb: manager._vehicle.unlock(cb))
    await settle(hass)
    assert client.frames == frames

    # A renamed charging action still works and updates reported telemetry.
    await manager.async_command(
        "Adjust charge current", lambda cb: manager._vehicle.set_charging_amps(12, cb), required_access="charging"
    )
    await settle_until(hass, lambda: state_of(hass, "number", f"{VIN}_number_charge_amps") == "12")


async def test_monitor_refresh_does_not_wake_sleeping_vehicle(hass, sim, monkeypatch, hass_storage):
    sim.set_sleeping(True)
    entry, _ = await setup_entry(hass, sim, monkeypatch, hass_storage, role="VEHICLE_MONITOR")
    manager = entry.runtime_data
    await manager.async_set_polling(False)
    refresh_id = er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{VIN}_button_force_update")
    # Initially the library has no observed sleep state. Its implicit wake path
    # must be suppressed just as when the car is known to be asleep.
    for _ in range(2):
        await hass.services.async_call("button", "press", {"entity_id": refresh_id}, blocking=True)
        await settle_until(hass, lambda: manager._vehicle.pending_command_count == 0)
        assert state_of(hass, "sensor", f"{VIN}_sensor_battery_level") == "unknown"
        assert state_of(hass, "binary_sensor", f"{VIN}_binary_asleep") == "on"
        assert hass.states.get(refresh_id).name.endswith("Refresh without waking")
        assert "Wake the car manually" in state_of(hass, "sensor", f"{VIN}_sensor_last_command")

    sim.set_sleeping(False)
    await hass.services.async_call("button", "press", {"entity_id": refresh_id}, blocking=True)
    await settle_until(hass, lambda: state_of(hass, "sensor", f"{VIN}_sensor_battery_level") == "55")
    assert state_of(hass, "binary_sensor", f"{VIN}_binary_asleep") == "off"


@pytest.mark.parametrize("reason", ["GENERICERROR_UNAUTHORIZED", "MESSAGEFAULT_ERROR_INSUFFICIENT_PRIVILEGES"])
async def test_denied_wake_explains_next_step(hass, sim, monkeypatch, hass_storage, reason):
    """Firmware permission failures must offer guidance rather than raw codes."""
    # The simulator doesn't implement role-specific firmware restrictions.
    monkeypatch.setattr(
        tesla_ble.Vehicle,
        "wake",
        lambda vehicle, callback: callback(
            SimpleNamespace(success=False, skipped=False, error_message=reason, may_have_succeeded=False)
        ),
    )
    entry, _ = await setup_entry(hass, sim, monkeypatch, hass_storage, role="CHARGING_MANAGER")
    wake_id = er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{VIN}_button_wake")
    await hass.services.async_call("button", "press", {"entity_id": wake_id}, blocking=True)
    message = state_of(hass, "sensor", f"{VIN}_sensor_last_command")
    assert "Wake the car manually" in message
    assert "key permissions" in message
    assert reason not in message
