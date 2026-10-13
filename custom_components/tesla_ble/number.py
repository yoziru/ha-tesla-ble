"""Number entities: charge limit and charging amps.

Both ranges come from the vehicle itself (charge_limit_soc_min/max and
charge_current_request_max), matching how the native Tesla integrations
expose them; the constants below only cover the moment before the first
telemetry arrives.
"""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE, UnitOfElectricCurrent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import TeslaBLEConfigEntry
from .entity import TeslaBLEEntity
from .manager import TeslaBLEManager

DEFAULT_CHARGE_LIMIT_MIN = 50
DEFAULT_CHARGE_LIMIT_MAX = 100
DEFAULT_CHARGING_AMPS_MAX = 48


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TeslaBLEConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    manager: TeslaBLEManager = entry.runtime_data
    if not manager.can_control_charging:
        return
    async_add_entities(
        [
            TeslaChargingLimitNumber(manager),
            TeslaChargingAmpsNumber(manager),
        ]
    )


class TeslaChargingLimitNumber(TeslaBLEEntity, NumberEntity):
    _attr_translation_key = "charge_limit"
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.AUTO

    def __init__(self, manager: TeslaBLEManager) -> None:
        super().__init__(manager, "number_charge_limit", "charge_state")

    @property
    def native_value(self) -> float | None:
        return self.manager.charge_state.get("charge_limit_soc")

    @property
    def native_min_value(self) -> float:
        value = self.manager.charge_state.get("charge_limit_soc_min")
        return value if value is not None else DEFAULT_CHARGE_LIMIT_MIN

    @property
    def native_max_value(self) -> float:
        value = self.manager.charge_state.get("charge_limit_soc_max")
        return value if value is not None else DEFAULT_CHARGE_LIMIT_MAX

    async def async_set_native_value(self, value: float) -> None:
        await self.manager.async_set_charging_limit(int(value))


class TeslaChargingAmpsNumber(TeslaBLEEntity, NumberEntity):
    _attr_translation_key = "charge_amps"
    _attr_native_min_value = 0
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_device_class = NumberDeviceClass.CURRENT
    _attr_mode = NumberMode.AUTO

    def __init__(self, manager: TeslaBLEManager) -> None:
        super().__init__(manager, "number_charge_amps", "charge_state")

    @property
    def native_value(self) -> float | None:
        return self.manager.charge_state.get("charging_amps")

    @property
    def native_max_value(self) -> float:
        value = self.manager.charge_state.get("charge_current_request_max")
        return value if value is not None else DEFAULT_CHARGING_AMPS_MAX

    async def async_set_native_value(self, value: float) -> None:
        await self.manager.async_set_charging_amps(int(value))
