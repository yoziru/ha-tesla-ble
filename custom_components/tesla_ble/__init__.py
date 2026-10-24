"""Tesla BLE integration: control a Tesla vehicle over Bluetooth LE.

Uses the TeslaBLE C++ library through its Python bindings and Home
Assistant's bluetooth stack, so local adapters and ESPHome Bluetooth
proxies both work.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import entity_registry as er

from .const import CONF_VIN, TeslaBLEConfigEntry
from .manager import TeslaBLEManager

PLATFORMS = [
    "binary_sensor",
    "button",
    "climate",
    "cover",
    "device_tracker",
    "lock",
    "number",
    "sensor",
    "select",
    "switch",
    "update",
]


async def async_migrate_entry(hass: HomeAssistant, entry: TeslaBLEConfigEntry) -> bool:
    """Migrate the config entry."""
    if entry.version > 1:
        return False
    if entry.minor_version < 2:
        # The power sensor moved from kW to W; reset a legacy kW override once
        # so later choices in the entity settings are left alone.
        registry = er.async_get(hass)
        power_id = registry.async_get_entity_id("sensor", "tesla_ble", f"{entry.data[CONF_VIN]}_sensor_charger_power")
        if power_id:
            power_options = registry.async_get(power_id).options.get("sensor", {})
            if power_options.get("unit_of_measurement") == "kW":
                registry.async_update_entity_options(power_id, "sensor", {**power_options, "unit_of_measurement": "W"})
        hass.config_entries.async_update_entry(entry, minor_version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: TeslaBLEConfigEntry) -> bool:
    manager = TeslaBLEManager(hass, entry)
    try:
        await manager.async_setup()
    except HomeAssistantError as err:
        raise ConfigEntryNotReady(str(err)) from err
    entry.runtime_data = manager
    if manager._key_rejected:
        entry.async_start_reauth(hass)
    registry = er.async_get(hass)
    if "steering_wheel_heater" not in manager.capabilities:
        steering_id = registry.async_get_entity_id("switch", "tesla_ble", f"{manager.vin}_switch_steering_wheel_heat")
        if steering_id and registry.async_get(steering_id).disabled_by is None:
            registry.async_update_entity(steering_id, disabled_by=er.RegistryEntryDisabler.INTEGRATION)
    for domain, key in (
        ("sensor", "software_update"),
        ("sensor", "charger_power_estimated"),
        ("sensor", "charge_limit_soc"),
        ("button", "software_update_install"),
        ("button", "software_update_cancel"),
        ("binary_sensor", "windows"),
        ("binary_sensor", "sunroof"),
        ("select", "seat_fan_front_left"),
        ("select", "seat_fan_front_right"),
        ("select", "seat_heater_left"),
        ("select", "seat_heater_right"),
        ("switch", "auto_seat_climate_left"),
        ("switch", "auto_seat_climate_right"),
        ("binary_sensor", "door_driver_front"),
        ("binary_sensor", "door_driver_rear"),
        ("binary_sensor", "door_passenger_front"),
        ("binary_sensor", "door_passenger_rear"),
        ("binary_sensor", "window_driver_front"),
        ("binary_sensor", "window_driver_rear"),
        ("binary_sensor", "window_passenger_front"),
        ("binary_sensor", "window_passenger_rear"),
    ):
        prefix = "binary" if domain == "binary_sensor" else domain
        if entity_id := registry.async_get_entity_id(domain, "tesla_ble", f"{manager.vin}_{prefix}_{key}"):
            registry.async_remove(entity_id)
    # Charging-detail sensors are ordinary, enabled entities now; re-enable any
    # that earlier versions registered as disabled-by-default.
    for key in (
        "sensor_evse_max_current",
        "sensor_vehicle_max_charge_current",
        "sensor_charge_current_request",
        "sensor_charger_phases",
    ):
        entity_id = registry.async_get_entity_id("sensor", "tesla_ble", f"{manager.vin}_{key}")
        if entity_id and registry.async_get(entity_id).disabled_by is er.RegistryEntryDisabler.INTEGRATION:
            registry.async_update_entity(entity_id, disabled_by=None)
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await manager.async_shutdown()
        raise
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TeslaBLEConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: TeslaBLEConfigEntry) -> None:
    from .storage import TeslaStore

    await TeslaStore(hass, entry.entry_id).async_remove()


async def _async_update_listener(hass: HomeAssistant, entry: TeslaBLEConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
