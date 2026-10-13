"""Lock entity: vehicle doors."""

from __future__ import annotations

from homeassistant.components.lock import LockEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import TeslaBLEConfigEntry
from .entity import TeslaBLEEntity
from .manager import TeslaBLEManager


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TeslaBLEConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    manager: TeslaBLEManager = entry.runtime_data
    if manager.can_control_vehicle:
        async_add_entities([TeslaDoorsLock(manager), TeslaChargePortLatchLock(manager)])


class TeslaDoorsLock(TeslaBLEEntity, LockEntity):
    _attr_translation_key = "doors"

    def __init__(self, manager: TeslaBLEManager) -> None:
        super().__init__(manager, "lock_doors", "vehicle_status")

    @property
    def is_locked(self) -> bool | None:
        return self.manager.vehicle_status.get("locked")

    async def async_lock(self, **kwargs) -> None:
        await self.manager.async_lock_doors(True)

    async def async_unlock(self, **kwargs) -> None:
        await self.manager.async_lock_doors(False)


LATCH_DISENGAGED = 2
LATCH_ENGAGED = 3


class TeslaChargePortLatchLock(TeslaBLEEntity, LockEntity):
    """Charge port cable latch: locked = engaged (cable secured)."""

    _attr_translation_key = "charge_port_latch"

    def __init__(self, manager: TeslaBLEManager) -> None:
        super().__init__(manager, "lock_charge_port_latch", "charge_state")

    @property
    def is_locked(self) -> bool | None:
        latch = self.manager.charge_state.get("charge_port_latch")
        if latch == LATCH_ENGAGED:
            return True
        if latch == LATCH_DISENGAGED:
            return False
        return None

    async def async_lock(self, **kwargs) -> None:
        await self.manager.async_close_charge_port()

    async def async_unlock(self, **kwargs) -> None:
        await self.manager.async_unlock_charge_port()
