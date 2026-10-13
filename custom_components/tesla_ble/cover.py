"""Cover entities: trunk, frunk, charge port door and windows."""

from __future__ import annotations

from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .binary_sensor import _any, _window_attributes
from .const import TeslaBLEConfigEntry
from .entity import TeslaBLEEntity, async_add_supported_entities
from .manager import TeslaBLEManager


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TeslaBLEConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    manager: TeslaBLEManager = entry.runtime_data
    if not manager.can_control_vehicle:
        return
    async_add_entities(
        [
            TeslaTrunkCover(manager),
            TeslaFrunkCover(manager),
            TeslaChargePortCover(manager),
            TeslaWindowsCover(manager),
        ]
    )
    async_add_supported_entities(
        manager, async_add_entities, [(TeslaSunroofCover(manager), ("sun_roof_percent_open",))]
    )


def _closure_open(manager: TeslaBLEManager, key: str) -> bool | None:
    value = manager.vehicle_status.get("closures", {}).get(key)
    if value is None:
        return None
    return value in ("open", "ajar", "opening")


class TeslaTrunkCover(TeslaBLEEntity, CoverEntity):
    _attr_translation_key = "trunk"
    _attr_device_class = CoverDeviceClass.DOOR
    _attr_supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE

    def __init__(self, manager: TeslaBLEManager) -> None:
        super().__init__(manager, "cover_trunk", "vehicle_status")

    @property
    def is_closed(self) -> bool | None:
        value = _closure_open(self.manager, "rear_trunk")
        return None if value is None else not value

    async def async_open_cover(self, **kwargs) -> None:
        await self.manager.async_open_trunk()

    async def async_close_cover(self, **kwargs) -> None:
        await self.manager.async_close_trunk()


class TeslaFrunkCover(TeslaBLEEntity, CoverEntity):
    _attr_translation_key = "frunk"
    _attr_device_class = CoverDeviceClass.DOOR
    _attr_supported_features = CoverEntityFeature.OPEN

    def __init__(self, manager: TeslaBLEManager) -> None:
        super().__init__(manager, "cover_frunk", "vehicle_status")

    @property
    def is_closed(self) -> bool | None:
        value = _closure_open(self.manager, "front_trunk")
        return None if value is None else not value

    async def async_open_cover(self, **kwargs) -> None:
        await self.manager.async_open_frunk()


class TeslaChargePortCover(TeslaBLEEntity, CoverEntity):
    _attr_translation_key = "charge_port"
    _attr_device_class = CoverDeviceClass.DOOR
    _attr_supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE

    def __init__(self, manager: TeslaBLEManager) -> None:
        super().__init__(manager, "cover_charge_port", "charge_state")

    @property
    def is_closed(self) -> bool | None:
        open_door = self.manager.charge_state.get("charge_port_door_open")
        if open_door is not None:
            return not open_door
        return None

    async def async_open_cover(self, **kwargs) -> None:
        await self.manager.async_open_charge_port()

    async def async_close_cover(self, **kwargs) -> None:
        await self.manager.async_close_charge_port()


class TeslaWindowsCover(TeslaBLEEntity, CoverEntity):
    _attr_translation_key = "windows"
    _attr_device_class = CoverDeviceClass.WINDOW
    _attr_supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE

    def __init__(self, manager: TeslaBLEManager) -> None:
        super().__init__(manager, "cover_windows", "closures_state")

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        return _window_attributes(self.manager)

    @property
    def is_closed(self) -> bool | None:
        values = [
            self.manager.closures_state.get(key)
            for key in (
                "window_open_driver_front",
                "window_open_passenger_front",
                "window_open_driver_rear",
                "window_open_passenger_rear",
            )
        ]
        opened = _any(values)
        return None if opened is None else not opened

    async def async_open_cover(self, **kwargs) -> None:
        await self.manager.async_vent_windows()

    async def async_close_cover(self, **kwargs) -> None:
        await self.manager.async_close_windows()


class TeslaSunroofCover(TeslaBLEEntity, CoverEntity):
    _attr_translation_key = "sunroof"
    _attr_icon = "mdi:car-select"
    _attr_device_class = CoverDeviceClass.WINDOW
    _attr_supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.SET_POSITION

    def __init__(self, manager: TeslaBLEManager) -> None:
        super().__init__(manager, "cover_sunroof", "closures_state")

    @property
    def current_cover_position(self) -> int | None:
        value = self.manager.closures_state.get("sun_roof_percent_open")
        return value if isinstance(value, int) and 0 <= value <= 100 else None

    @property
    def is_closed(self) -> bool | None:
        position = self.current_cover_position
        return None if position is None else position == 0

    async def async_open_cover(self, **kwargs) -> None:
        await self.manager.async_set_sunroof_position(100)

    async def async_close_cover(self, **kwargs) -> None:
        await self.manager.async_set_sunroof_position(0)

    async def async_set_cover_position(self, **kwargs) -> None:
        await self.manager.async_set_sunroof_position(kwargs["position"])
