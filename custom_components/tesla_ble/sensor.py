"""Sensor entities: vehicle state values and text states."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfLength,
    UnitOfPower,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

import tesla_ble

from .const import ROLE_LABELS, TeslaBLEConfigEntry
from .entity import TeslaBLEEntity, async_add_supported_entities
from .manager import TeslaBLEManager, charger_power_w

CHARGING_STATE_OPTIONS = [
    "Unknown",
    "Disconnected",
    "No Power",
    "Starting",
    "Charging",
    "Complete",
    "Stopped",
    "Calibrating",
]
SHIFT_STATE_OPTIONS = ["P", "R", "N", "D", "SNA", "Invalid", "Unknown"]
IEC61851_OPTIONS = ["A", "B", "C", "E", "F", "Unknown"]
CHARGE_LIMIT_REASON_OPTIONS = ["None", "EVSE", "BattTempLow", "HighSoc", "Cabin", "Unknown"]


def _odometer_miles(manager: TeslaBLEManager) -> float | None:
    value = manager.drive_state.get("odometer_in_hundredths_of_a_mile")
    return None if value is None else value / 100.0


def _charger_power(manager: TeslaBLEManager) -> float | None:
    """AC charging power from the car's voltage/current/phase readings, with a raw fallback."""
    return charger_power_w(manager.charge_state)


def _iec61851_state(manager: TeslaBLEManager) -> str | None:
    raw = manager.charge_state.get("charging_state_raw")
    return None if raw is None else tesla_ble.iec61851_state_text(raw)


def _battery_attributes(manager: TeslaBLEManager) -> dict[str, Any] | None:
    soc = manager.charge_state.get("battery_level")
    return None if soc is None else {"raw_soc": soc}


def _range_attributes(manager: TeslaBLEManager) -> dict[str, Any] | None:
    est = manager.charge_state.get("est_battery_range")
    if est is None:
        return None
    return {"est_battery_range_miles": est, "est_battery_range_km": round(est * 1.609344, 2)}


def _energy_attributes(manager: TeslaBLEManager) -> dict[str, Any] | None:
    rated = manager.charge_state.get("charge_miles_added_rated")
    ideal = manager.charge_state.get("charge_miles_added_ideal")
    return {
        "session_energy_kwh": manager.charge_state.get("charge_energy_added"),
        "tracking_since": manager.energy_tracking_since,
        "added_range": rated if rated is not None else ideal,
    }


def _charger_power_attributes(manager: TeslaBLEManager) -> dict[str, Any] | None:
    state = manager.charge_state
    values = {
        "charger_amps_request": state.get("charge_current_request"),
        "charger_amps_actual": state.get("charger_actual_current"),
        "charger_volts": state.get("charger_voltage"),
        "charger_phases": state.get("charger_phases"),
        "charger_max_current": state.get("charger_pilot_current"),
        "vehicle_max_current": state.get("charge_current_request_max"),
        "reported_power_w": None if state.get("charger_power") is None else state["charger_power"] * 1000,
    }
    if all(value is None for value in values.values()):
        return None
    return values


def _shift_state_attributes(manager: TeslaBLEManager) -> dict[str, Any] | None:
    raw = manager.drive_state.get("shift_state_raw")
    return None if raw is None else {"raw_state": raw}


def _time_charge_complete(manager: TeslaBLEManager) -> datetime | None:
    minutes = manager.charge_state.get("minutes_to_full_charge")
    observed_at = manager.state_observed_at.get("charge_state")
    if minutes is None or minutes <= 0 or observed_at is None:
        return None
    return observed_at + timedelta(minutes=minutes)


def _steering_heat_mode(manager: TeslaBLEManager) -> str | None:
    state = manager.climate_state
    if state.get("auto_steering_wheel_heat") is True:
        return "Auto"
    if "steering_wheel_heat_level" in state:
        return {0: "Unknown", 1: "Off", 2: "Low", 3: "High"}.get(state["steering_wheel_heat_level"], "Unknown")
    enabled = state.get("steering_wheel_heater")
    return None if enabled is None else "On" if enabled else "Off"


@dataclass(frozen=True, kw_only=True)
class TeslaSensorDescription(SensorEntityDescription):
    value_fn: Callable[[TeslaBLEManager], Any]
    attrs_fn: Callable[[TeslaBLEManager], dict[str, Any] | None] | None = None
    state: str | None = "charge_state"
    supported_by: tuple[str, ...] = ()


SENSOR_DESCRIPTIONS: tuple[TeslaSensorDescription, ...] = (
    TeslaSensorDescription(
        key="key_access",
        icon="mdi:key-wireless",
        state=None,
        value_fn=lambda m: ROLE_LABELS[m.role],
        device_class=SensorDeviceClass.ENUM,
        options=list(ROLE_LABELS.values()),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    TeslaSensorDescription(
        key="steering_wheel_heat_mode",
        icon="mdi:steering",
        state="climate_state",
        value_fn=_steering_heat_mode,
        device_class=SensorDeviceClass.ENUM,
        options=["Off", "On", "Low", "High", "Auto", "Unknown"],
        supported_by=("steering_wheel_heat_level", "auto_steering_wheel_heat"),
    ),
    TeslaSensorDescription(
        key="battery_level",
        icon="mdi:battery",
        value_fn=lambda m: m.charge_state.get("battery_level"),
        attrs_fn=_battery_attributes,
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    TeslaSensorDescription(
        key="battery_range",
        icon="mdi:map-marker-distance",
        value_fn=lambda m: m.charge_state.get("battery_range"),
        attrs_fn=_range_attributes,
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.MILES,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    TeslaSensorDescription(
        key="charge_energy",
        icon="mdi:transmission-tower",
        state=None,
        value_fn=lambda m: m.ac_energy_total,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
    ),
    TeslaSensorDescription(
        key="charge_energy_added",
        icon="mdi:lightning-bolt",
        state=None,
        value_fn=lambda m: m.energy_total,
        attrs_fn=_energy_attributes,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    TeslaSensorDescription(
        key="minutes_to_full_charge",
        icon="mdi:battery-clock-outline",
        value_fn=_time_charge_complete,
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
    TeslaSensorDescription(
        key="charger_power",
        icon="mdi:flash",
        value_fn=_charger_power,
        attrs_fn=_charger_power_attributes,
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    TeslaSensorDescription(
        key="charger_voltage",
        icon="mdi:sine-wave",
        value_fn=lambda m: m.charge_state.get("charger_voltage"),
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    TeslaSensorDescription(
        key="charger_actual_current",
        icon="mdi:current-ac",
        value_fn=lambda m: m.charge_state.get("charger_actual_current"),
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    TeslaSensorDescription(
        key="evse_max_current",
        icon="mdi:current-ac",
        value_fn=lambda m: m.charge_state.get("charger_pilot_current"),
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    TeslaSensorDescription(
        key="vehicle_max_charge_current",
        icon="mdi:current-ac",
        value_fn=lambda m: m.charge_state.get("charge_current_request_max"),
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    TeslaSensorDescription(
        key="charge_current_request",
        icon="mdi:current-ac",
        value_fn=lambda m: m.charge_state.get("charge_current_request"),
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    TeslaSensorDescription(
        key="charger_phases",
        icon="mdi:sine-wave",
        value_fn=lambda m: m.charge_state.get("charger_phases"),
        state_class=SensorStateClass.MEASUREMENT,
    ),
    TeslaSensorDescription(
        key="charging_rate",
        icon="mdi:speedometer",
        value_fn=lambda m: m.charge_state.get("charge_rate_mph"),
        device_class=SensorDeviceClass.SPEED,
        native_unit_of_measurement=UnitOfSpeed.MILES_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    TeslaSensorDescription(
        key="odometer",
        icon="mdi:counter",
        state="drive_state",
        value_fn=_odometer_miles,
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.MILES,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    TeslaSensorDescription(
        key="outside_temp",
        icon="mdi:thermometer",
        state="climate_state",
        value_fn=lambda m: m.climate_state.get("outside_temp_celsius"),
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    TeslaSensorDescription(
        key="inside_temp",
        icon="mdi:thermometer",
        state="climate_state",
        value_fn=lambda m: m.climate_state.get("inside_temp_celsius"),
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    TeslaSensorDescription(
        key="tpms_front_left",
        icon="mdi:tire",
        state="tire_pressure_state",
        value_fn=lambda m: m.tire_pressure_state.get("tpms_pressure_fl"),
        device_class=SensorDeviceClass.PRESSURE,
        native_unit_of_measurement=UnitOfPressure.BAR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    TeslaSensorDescription(
        key="tpms_front_right",
        icon="mdi:tire",
        state="tire_pressure_state",
        value_fn=lambda m: m.tire_pressure_state.get("tpms_pressure_fr"),
        device_class=SensorDeviceClass.PRESSURE,
        native_unit_of_measurement=UnitOfPressure.BAR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    TeslaSensorDescription(
        key="tpms_rear_left",
        icon="mdi:tire",
        state="tire_pressure_state",
        value_fn=lambda m: m.tire_pressure_state.get("tpms_pressure_rl"),
        device_class=SensorDeviceClass.PRESSURE,
        native_unit_of_measurement=UnitOfPressure.BAR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    TeslaSensorDescription(
        key="tpms_rear_right",
        icon="mdi:tire",
        state="tire_pressure_state",
        value_fn=lambda m: m.tire_pressure_state.get("tpms_pressure_rr"),
        device_class=SensorDeviceClass.PRESSURE,
        native_unit_of_measurement=UnitOfPressure.BAR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    TeslaSensorDescription(
        key="charging_state",
        icon="mdi:ev-station",
        value_fn=lambda m: m.charge_state.get("charging_state"),
        device_class=SensorDeviceClass.ENUM,
        options=CHARGING_STATE_OPTIONS,
    ),
    TeslaSensorDescription(
        key="shift_state",
        icon="mdi:car-shift-pattern",
        state="drive_state",
        value_fn=lambda m: m.drive_state.get("shift_state"),
        attrs_fn=_shift_state_attributes,
        device_class=SensorDeviceClass.ENUM,
        options=SHIFT_STATE_OPTIONS,
    ),
    TeslaSensorDescription(
        key="iec61851_state",
        icon="mdi:ev-station",
        value_fn=_iec61851_state,
        device_class=SensorDeviceClass.ENUM,
        options=IEC61851_OPTIONS,
    ),
    TeslaSensorDescription(
        key="charge_limit_reason",
        icon="mdi:information-outline",
        value_fn=lambda m: m.charge_state.get("charge_limit_reason"),
        device_class=SensorDeviceClass.ENUM,
        options=CHARGE_LIMIT_REASON_OPTIONS,
    ),
    TeslaSensorDescription(
        key="last_command",
        icon="mdi:console-line",
        state=None,
        value_fn=lambda m: m.last_command,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    TeslaSensorDescription(
        key="ble_signal",
        icon="mdi:bluetooth",
        state=None,
        value_fn=lambda m: m.rssi,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TeslaBLEConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    manager: TeslaBLEManager = entry.runtime_data
    if not manager.can_control_charging:
        async_add_entities(
            TeslaSensor(manager, description)
            for description in (
                TeslaSensorDescription(
                    key="charge_limit",
                    value_fn=lambda m: m.charge_state.get("charge_limit_soc"),
                    native_unit_of_measurement=PERCENTAGE,
                ),
                TeslaSensorDescription(
                    key="charging_amps",
                    value_fn=lambda m: m.charge_state.get("charging_amps"),
                    device_class=SensorDeviceClass.CURRENT,
                    native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
                ),
            )
        )
    async_add_entities(
        TeslaSensor(manager, description) for description in SENSOR_DESCRIPTIONS if not description.supported_by
    )
    async_add_supported_entities(
        manager,
        async_add_entities,
        [
            (TeslaSensor(manager, description), description.supported_by)
            for description in SENSOR_DESCRIPTIONS
            if description.supported_by
        ],
    )


class TeslaSensor(TeslaBLEEntity, SensorEntity):
    entity_description: TeslaSensorDescription
    _unrecorded_attributes = frozenset(
        {
            "raw_soc",
            "est_battery_range_miles",
            "est_battery_range_km",
            "added_range",
            "session_energy_kwh",
            "charger_amps_request",
            "charger_amps_actual",
            "charger_volts",
            "charger_phases",
            "charger_max_current",
            "vehicle_max_current",
            "reported_power_w",
            "raw_state",
        }
    )

    def __init__(self, manager: TeslaBLEManager, description: TeslaSensorDescription) -> None:
        super().__init__(manager, f"sensor_{description.key}", description.state)
        self.entity_description = description
        self._attr_translation_key = description.key

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.manager)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.manager)
