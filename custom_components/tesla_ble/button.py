"""Button entities: wake, pair, refresh, and remote actions."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
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
    entities = [TeslaPairButton(manager), TeslaForceUpdateButton(manager), TeslaRegenerateKeyButton(manager)]
    if manager.can_wake:
        entities.append(TeslaWakeButton(manager))
    if manager.can_control_vehicle:
        entities.extend(
            [TeslaUnlatchDriverDoorButton(manager), TeslaFlashLightsButton(manager), TeslaHonkHornButton(manager)]
        )
    async_add_entities(entities)


class TeslaWakeButton(TeslaBLEEntity, ButtonEntity):
    _attr_translation_key = "wake"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, manager: TeslaBLEManager) -> None:
        super().__init__(manager, "button_wake")

    async def async_press(self) -> None:
        await self.manager.async_wake()


class TeslaPairButton(TeslaBLEEntity, ButtonEntity):
    """Start pairing: press this, then tap a key card on the console."""

    _attr_translation_key = "pair"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, manager: TeslaBLEManager) -> None:
        super().__init__(manager, "button_pair")

    async def async_press(self) -> None:
        await self.manager.async_pair()


class TeslaForceUpdateButton(TeslaBLEEntity, ButtonEntity):
    _attr_translation_key = "force_update"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, manager: TeslaBLEManager) -> None:
        super().__init__(manager, "button_force_update")
        if not manager.can_wake:
            self._attr_translation_key = "refresh_without_waking"

    async def async_press(self) -> None:
        await self.manager.async_force_update()


class TeslaFlashLightsButton(TeslaBLEEntity, ButtonEntity):
    _attr_translation_key = "flash_lights"

    def __init__(self, manager: TeslaBLEManager) -> None:
        super().__init__(manager, "button_flash_lights")

    async def async_press(self) -> None:
        await self.manager.async_flash_lights()


class TeslaHonkHornButton(TeslaBLEEntity, ButtonEntity):
    _attr_translation_key = "honk_horn"

    def __init__(self, manager: TeslaBLEManager) -> None:
        super().__init__(manager, "button_honk_horn")

    async def async_press(self) -> None:
        await self.manager.async_honk_horn()


class TeslaRegenerateKeyButton(TeslaBLEEntity, ButtonEntity):
    _attr_translation_key = "regenerate_key"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, manager: TeslaBLEManager) -> None:
        super().__init__(manager, "button_regenerate_key")

    async def async_press(self) -> None:
        await self.manager.async_regenerate_key()


class TeslaUnlatchDriverDoorButton(TeslaBLEEntity, ButtonEntity):
    _attr_translation_key = "unlatch_driver_door"
    _attr_entity_registry_enabled_default = False

    def __init__(self, manager: TeslaBLEManager) -> None:
        super().__init__(manager, "button_unlatch_driver_door")

    async def async_press(self) -> None:
        await self.manager.async_unlatch_driver_door()
