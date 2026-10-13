"""Climate entity: Tesla HVAC, presets and bioweapon mode."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import TeslaBLEConfigEntry
from .entity import TeslaBLEEntity
from .manager import TeslaBLEManager

# Raw CarServer_ClimateState tags reported by the vehicle
KEEPER_OFF = 2
KEEPER_ON = 3
KEEPER_DOG = 4
KEEPER_CAMP = 5
DEFROST_MAX = 3

PRESET_NORMAL = "Normal"
PRESET_DEFROST = "Defrost"
PRESET_KEEP_ON = "Keep On"
PRESET_DOG = "Dog Mode"
PRESET_CAMP = "Camp Mode"

PRESET_TO_KEEPER = {
    PRESET_NORMAL: 0,
    PRESET_KEEP_ON: 1,
    PRESET_DOG: 2,
    PRESET_CAMP: 3,
}

FAN_NORMAL = "Normal"
FAN_BIOWEAPON = "Bioweapon Mode"

DEFAULT_MIN_TEMP = 15.0
DEFAULT_MAX_TEMP = 28.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TeslaBLEConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    if entry.runtime_data.can_control_vehicle:
        async_add_entities([TeslaClimate(entry.runtime_data)])


class TeslaClimate(TeslaBLEEntity, ClimateEntity):
    _attr_translation_key = "climate"
    _attr_icon = "mdi:air-conditioner"
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT_COOL]
    _attr_preset_modes = [PRESET_NORMAL, PRESET_DEFROST, PRESET_KEEP_ON, PRESET_DOG, PRESET_CAMP]
    _attr_fan_modes = [FAN_NORMAL, FAN_BIOWEAPON]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE | ClimateEntityFeature.FAN_MODE
    )
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.5

    def __init__(self, manager: TeslaBLEManager) -> None:
        super().__init__(manager, "climate_climate", "climate_state")

    @property
    def _state(self) -> dict[str, Any]:
        return self.manager.climate_state

    @property
    def hvac_mode(self) -> HVACMode | None:
        is_on = self._state.get("is_climate_on")
        if is_on is None:
            return None
        return HVACMode.HEAT_COOL if is_on else HVACMode.OFF

    @property
    def current_temperature(self) -> float | None:
        return self._state.get("inside_temp_celsius")

    @property
    def target_temperature(self) -> float | None:
        return self._state.get("driver_temp_setting")

    @property
    def min_temp(self) -> float:
        return self._state.get("min_avail_temp_celsius", DEFAULT_MIN_TEMP)

    @property
    def max_temp(self) -> float:
        return self._state.get("max_avail_temp_celsius", DEFAULT_MAX_TEMP)

    @property
    def preset_mode(self) -> str | None:
        keeper = self._state.get("climate_keeper_mode")
        if keeper == KEEPER_DOG:
            return PRESET_DOG
        if keeper == KEEPER_CAMP:
            return PRESET_CAMP
        if keeper == KEEPER_ON:
            return PRESET_KEEP_ON
        if self._state.get("defrost_mode") == DEFROST_MAX:
            return PRESET_DEFROST
        if keeper in (None, KEEPER_OFF):
            return PRESET_NORMAL if keeper is not None else None
        return None

    @property
    def fan_mode(self) -> str | None:
        bioweapon = self._state.get("bioweapon_mode_on")
        if bioweapon is None:
            return None
        return FAN_BIOWEAPON if bioweapon else FAN_NORMAL

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self.manager.async_set_climate(hvac_mode != HVACMode.OFF)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        await self.manager.async_set_climate_temp(float(temperature))

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if preset_mode == PRESET_DEFROST:
            await self.manager.async_set_climate_keeper(0)
            await self.manager.async_set_preconditioning_max(True)
            return
        keeper = PRESET_TO_KEEPER.get(preset_mode)
        if keeper is None:
            return
        await self.manager.async_set_preconditioning_max(False)
        await self.manager.async_set_climate_keeper(keeper)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        await self.manager.async_set_bioweapon_mode(fan_mode == FAN_BIOWEAPON)
