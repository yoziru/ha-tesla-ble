"""Capability-aware seat climate and cabin protection controls.

A seat has one climate control, matching the vehicle: the same three levels
are heating or cooling, never both. Cooling options appear only for seats the
vehicle reports as ventilated.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

import tesla_ble

from .const import TeslaBLEConfigEntry
from .entity import TeslaBLEEntity, async_add_supported_entities
from .manager import TeslaBLEManager

HEAT_OPTIONS = ("Low heat", "Medium heat", "High heat")
COOL_OPTIONS = ("Low cool", "Medium cool", "High cool")

# Seat translation key -> (seat position, heater field, cooling field or None, auto field or None)
SEATS = {
    "seat_climate_left": (
        tesla_ble.SeatPosition.FRONT_LEFT,
        "seat_heater_left",
        "seat_fan_front_left",
        "auto_seat_climate_left",
    ),
    "seat_climate_right": (
        tesla_ble.SeatPosition.FRONT_RIGHT,
        "seat_heater_right",
        "seat_fan_front_right",
        "auto_seat_climate_right",
    ),
    "seat_heater_rear_left": (tesla_ble.SeatPosition.REAR_LEFT, "seat_heater_rear_left", None, None),
    "seat_heater_rear_right": (tesla_ble.SeatPosition.REAR_RIGHT, "seat_heater_rear_right", None, None),
    "seat_heater_rear_center": (tesla_ble.SeatPosition.REAR_CENTER, "seat_heater_rear_center", None, None),
    "seat_heater_rear_left_back": (tesla_ble.SeatPosition.REAR_LEFT_BACK, "seat_heater_rear_left_back", None, None),
    "seat_heater_rear_right_back": (tesla_ble.SeatPosition.REAR_RIGHT_BACK, "seat_heater_rear_right_back", None, None),
    "seat_heater_third_row_left": (tesla_ble.SeatPosition.THIRD_ROW_LEFT, "seat_heater_third_row_left", None, None),
    "seat_heater_third_row_right": (tesla_ble.SeatPosition.THIRD_ROW_RIGHT, "seat_heater_third_row_right", None, None),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: TeslaBLEConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    manager = entry.runtime_data
    if not manager.can_control_vehicle:
        return
    entities = [
        (
            TeslaSeatClimateSelect(manager, key, seat, heat_field, cool_field, auto_field),
            tuple(field for field in (heat_field, cool_field, auto_field) if field),
        )
        for key, (seat, heat_field, cool_field, auto_field) in SEATS.items()
    ]
    entities.extend(
        [
            (TeslaCabinProtectionSelect(manager), ("cabin_overheat_protection",)),
            (TeslaCabinProtectionTemperatureSelect(manager), ("cop_activation_temperature",)),
        ]
    )
    async_add_supported_entities(manager, async_add_entities, entities)


class TeslaSeatClimateSelect(TeslaBLEEntity, SelectEntity):
    """One seat control: heat levels, or cool levels on ventilated seats."""

    def __init__(
        self,
        manager: TeslaBLEManager,
        key: str,
        seat: tesla_ble.SeatPosition,
        heat_field: str,
        cool_field: str | None,
        auto_field: str | None,
    ) -> None:
        super().__init__(manager, f"select_{key}", "climate_state")
        self._heat_field = heat_field
        self._cool_field = cool_field
        self._auto_field = auto_field
        self._seat = seat
        self._attr_translation_key = key

    @property
    def _auto(self) -> bool:
        return bool(self._auto_field and self.manager.climate_state.get(self._auto_field) is True)

    @property
    def _heat_level(self) -> int:
        level = self.manager.climate_state.get(self._heat_field)
        return level if isinstance(level, int) else 0

    @property
    def _cool_level(self) -> int:
        level = self.manager.climate_state.get(self._cool_field) if self._cool_field else None
        return level if isinstance(level, int) else 0

    @property
    def options(self) -> list[str]:
        options = ["Off", *HEAT_OPTIONS]
        if self._cool_field and self._cool_field in self.manager.capabilities:
            options.extend(COOL_OPTIONS)
        if self._auto_field and self._auto_field in self.manager.capabilities:
            options.append("Auto")
        return options

    @property
    def icon(self) -> str:
        if self._cool_level > 0:
            return "mdi:car-seat-cooler"
        if self._heat_level > 0:
            return "mdi:car-seat-heater"
        return "mdi:car-seat"

    @property
    def current_option(self) -> str | None:
        if self._auto:
            return "Auto"
        state = self.manager.climate_state
        heat = state.get(self._heat_field)
        cool = state.get(self._cool_field) if self._cool_field else None
        if isinstance(heat, int) and 1 <= heat <= 3:
            return HEAT_OPTIONS[heat - 1]
        if isinstance(cool, int) and 1 <= cool <= 3:
            return COOL_OPTIONS[cool - 1]
        if heat == 0 or cool == 0:
            return "Off"
        return None

    async def async_select_option(self, option: str) -> None:
        if option == "Auto":
            await self.manager.async_set_auto_seat_climate(self._seat, True)
        elif option in HEAT_OPTIONS:
            await self.manager.async_set_seat_climate(self._seat, HEAT_OPTIONS.index(option) + 1, False)
        elif option in COOL_OPTIONS:
            await self.manager.async_set_seat_climate(self._seat, COOL_OPTIONS.index(option) + 1, True)
        else:
            await self.manager.async_set_seat_climate(self._seat, 0, False)


class TeslaCabinProtectionSelect(TeslaBLEEntity, SelectEntity):
    _attr_translation_key = "cabin_overheat_protection"
    _attr_icon = "mdi:car-defrost-front"

    def __init__(self, manager: TeslaBLEManager) -> None:
        super().__init__(manager, "select_cabin_overheat_protection", "climate_state")

    @property
    def options(self) -> list[str]:
        state = self.manager.climate_state
        supports_fan = (
            state.get("supports_fan_only_cabin_overheat_protection") or state.get("cabin_overheat_protection") == 2
        )
        return ["Off", "On", "Fan only"] if supports_fan else ["Off", "On"]

    @property
    def current_option(self) -> str | None:
        mode = self.manager.climate_state.get("cabin_overheat_protection")
        return {0: "Off", 1: "On", 2: "Fan only"}.get(mode)

    @property
    def extra_state_attributes(self) -> dict:
        state = self.manager.climate_state
        return {
            key: state.get(key)
            for key in (
                "allow_cabin_overheat_protection",
                "supports_fan_only_cabin_overheat_protection",
                "cabin_overheat_protection_actively_cooling",
                "cop_not_running_reason",
            )
        }

    async def async_select_option(self, option: str) -> None:
        if option != "Off" and self.manager.climate_state.get("allow_cabin_overheat_protection") is False:
            raise HomeAssistantError("Cabin overheat protection is not currently allowed by the vehicle")
        await self.manager.async_set_cabin_overheat_protection(["Off", "On", "Fan only"].index(option))


class TeslaCabinProtectionTemperatureSelect(TeslaBLEEntity, SelectEntity):
    _attr_translation_key = "cop_activation_temperature"
    _attr_icon = "mdi:thermometer"
    _attr_options = ["Low", "Medium", "High"]

    def __init__(self, manager: TeslaBLEManager) -> None:
        super().__init__(manager, "select_cop_activation_temperature", "climate_state")

    @property
    def current_option(self) -> str | None:
        return {1: "Low", 2: "Medium", 3: "High"}.get(self.manager.climate_state.get("cop_activation_temperature"))

    async def async_select_option(self, option: str) -> None:
        await self.manager.async_set_cabin_overheat_temperature(self.options.index(option) + 1)
