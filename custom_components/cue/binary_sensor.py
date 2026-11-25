"""The "Cue open" binary sensor."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import CueConfigEntry, CueCoordinator
from .entity import CueEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CueConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the sensor."""
    async_add_entities([CueOpenSensor(entry.runtime_data)])


class CueOpenSensor(CueEntity, BinarySensorEntity):
    """On while Cue answers on the LAN, which on tvOS means it's frontmost
    (or was very recently: tvOS suspends it soon after it leaves)."""

    _attr_translation_key = "cue_open"

    def __init__(self, coordinator: CueCoordinator) -> None:
        super().__init__(coordinator, "cue_open")

    @property
    def is_on(self) -> bool:
        """Reachable."""
        data = self.coordinator.data
        return bool(data and data.reachable)
