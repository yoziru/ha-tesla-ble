"""Diagnostics support for the Tesla BLE integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import TeslaBLEConfigEntry


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: TeslaBLEConfigEntry) -> dict[str, Any]:
    manager = entry.runtime_data
    return async_redact_data(
        {
            "entry": {
                "data": dict(entry.data),
                "options": dict(entry.options),
            },
            "connection": {
                "connected": manager.connected,
                "address": manager.address,
            },
            "last_command": manager.last_command,
            "configured_key_role": manager.role,
            "polling": manager.polling_diagnostics,
            "state": {
                "vehicle_status": manager.vehicle_status,
                "charge_state": manager.charge_state,
                "climate_state": manager.climate_state,
                "drive_state": manager.drive_state,
                "closures_state": manager.closures_state,
                "tire_pressure_state": manager.tire_pressure_state,
            },
        },
        {"vin", "address", "latitude", "longitude"},
    )
