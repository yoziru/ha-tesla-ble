"""Vehicle firmware update, installation and cancellation."""

from __future__ import annotations

from typing import Any

from homeassistant.components.update import UpdateDeviceClass, UpdateEntity, UpdateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback

import tesla_ble

from .const import TeslaBLEConfigEntry
from .entity import TeslaBLEEntity
from .manager import TeslaBLEManager


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TeslaBLEConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([TeslaFirmwareUpdate(entry.runtime_data)])
    entity_platform.async_get_current_platform().async_register_entity_service(
        "cancel_software_update", {}, "async_cancel"
    )


class TeslaFirmwareUpdate(TeslaBLEEntity, UpdateEntity):
    _attr_translation_key = "software_update"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_title = "Tesla vehicle firmware"

    def __init__(self, manager: TeslaBLEManager) -> None:
        super().__init__(manager, "update_software_update", "software_update_state")

    @property
    def installed_version(self) -> str | None:
        return "Unknown" if self.manager.software_update_state.get("status") in range(1, 7) else None

    @property
    def latest_version(self) -> str | None:
        state = self.manager.software_update_state
        if state.get("status") == 1:
            return "Unknown"
        if state.get("status") in range(2, 7):
            return state.get("version") or "Pending update"
        return None

    @property
    def supported_features(self) -> UpdateEntityFeature:
        features = UpdateEntityFeature.PROGRESS
        if (
            self.manager.can_control_vehicle
            and self.manager.state_fresh("software_update_state")
            and self.manager.software_update_state.get("status") == 4
        ):
            features |= UpdateEntityFeature.INSTALL
        return features

    @property
    def in_progress(self) -> bool:
        return self.manager.software_update_state.get("status") in (2, 5, 6)

    @property
    def update_percentage(self) -> float | None:
        state = self.manager.software_update_state
        field = "install_perc" if state.get("status") == 2 else "download_perc"
        value = state.get(field)
        return None if value is None or not self.in_progress else max(0, min(100, value))

    @property
    def release_summary(self) -> str:
        status = tesla_ble.software_update_status_text(self.manager.software_update_state.get("status", 0))
        return f"{status}. The installed firmware version is not reported over BLE."

    @property
    def release_url(self) -> str | None:
        version = (self.manager.software_update_state.get("version") or "").split(" ")[0]
        if not version or self.manager.software_update_state.get("status") == 1:
            return None
        return f"https://www.notateslaapp.com/software-updates/version/{version}/release-notes"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.manager.software_update_state
        return {
            "status": tesla_ble.software_update_status_text(state.get("status", 0)),
            "can_cancel": (
                self.manager.can_control_vehicle
                and self.manager.state_fresh("software_update_state")
                and state.get("status") in (3, 5, 6)
            ),
            "expected_duration_sec": state.get("expected_duration_sec"),
        }

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        if version is not None or backup:
            raise HomeAssistantError(
                "Only the update offered by the vehicle can be installed; backups are not supported"
            )
        await self.manager.async_install_software_update()

    async def async_cancel(self) -> None:
        await self.manager.async_cancel_software_update()
