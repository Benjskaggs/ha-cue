"""Shared base for Cue entities."""

from __future__ import annotations

from typing import Any

from homeassistant.const import CONF_NAME
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_APP_VERSION, DEFAULT_NAME, DOMAIN, MANUFACTURER
from .coordinator import CueCoordinator


def entry_key(coordinator: CueCoordinator) -> str:
    """The id entities and the device hang off: the entry's unique_id.

    That's Cue's stable id (`/v1/info.id`, TXT `id`) from Slice B builds on,
    and the host for older builds. `migration.py` renames registry entries
    when an entry moves from the host to the id.
    """
    entry = coordinator.config_entry
    return entry.unique_id or entry.entry_id


class CueEntity(CoordinatorEntity[CueCoordinator]):
    """An entity on one Apple TV running Cue."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: CueCoordinator, key: str | None) -> None:
        super().__init__(coordinator)
        base = entry_key(coordinator)
        self._attr_unique_id = f"{base}_{key}" if key else base
        entry = coordinator.config_entry
        app = (coordinator.data.status or {}).get("app", {}) if coordinator.data else {}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, base)},
            name=entry.data.get(CONF_NAME) or DEFAULT_NAME,
            manufacturer=MANUFACTURER,
            model="Cue for Apple TV",
            sw_version=_version(app) or entry.data.get(CONF_APP_VERSION),
        )

    @property
    def player(self) -> dict[str, Any] | None:
        """The current `player` object, or None."""
        data = self.coordinator.data
        return data.player if data else None


def _version(app: dict[str, Any]) -> str | None:
    version = app.get("version")
    build = app.get("build")
    if version and build:
        return f"{version} ({build})"
    return version
