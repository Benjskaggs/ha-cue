"""The playback event entity: `event.<tv>_playback`."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import EVENT_TYPES
from .coordinator import CueConfigEntry, CueCoordinator
from .entity import CueEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CueConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the event entity."""
    async_add_entities([CuePlaybackEvent(entry.runtime_data)])


class CuePlaybackEvent(CueEntity, EventEntity):
    """Started, paused, resumed, stopped, ended, item changed, intro and
    credits started, and Cue leaving the screen. Pushed from Cue 1.2.1
    (within half a second), else worked out from the poll."""

    _attr_translation_key = "playback"
    _attr_event_types = list(EVENT_TYPES)

    def __init__(self, coordinator: CueCoordinator) -> None:
        super().__init__(coordinator, "playback")

    @property
    def available(self) -> bool:
        """Always: an event entity keeps its last event, and Cue leaving
        is itself an event."""
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_event_listener(self._on_event))

    @callback
    def _on_event(self, kind: str, data: dict[str, Any]) -> None:
        self._trigger_event(kind, data)
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Nothing to redraw: only events change this entity."""
