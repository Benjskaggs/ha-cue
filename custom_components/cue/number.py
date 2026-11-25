"""The subtitle timing (offset) number, for builds that list `subtitleOffset`."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import FEATURE_SUBTITLE_OFFSET, SUBTITLE_OFFSET_LIMIT
from .coordinator import CueConfigEntry, CueCoordinator
from .entity import CueEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CueConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the number when the build can shift subtitles."""
    coordinator = entry.runtime_data
    if coordinator.has(FEATURE_SUBTITLE_OFFSET):
        async_add_entities([CueSubtitleOffset(coordinator)])


class CueSubtitleOffset(CueEntity, NumberEntity):
    """The viewer's subtitle delay in seconds (positive = later).

    Set from here it applies to the stream that's playing only; Cue doesn't
    save it for the title. Available while subtitles are on.
    """

    _attr_translation_key = "subtitle_offset"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_native_min_value = -SUBTITLE_OFFSET_LIMIT
    _attr_native_max_value = SUBTITLE_OFFSET_LIMIT
    _attr_native_step = 0.05
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: CueCoordinator) -> None:
        super().__init__(coordinator, "subtitle_offset")

    def _subtitles(self) -> dict:
        block = (self.player or {}).get("subtitles")
        return block if isinstance(block, dict) else {}

    @property
    def available(self) -> bool:
        """While a subtitle track is on."""
        return super().available and self._subtitles().get("selected") is not None

    @property
    def native_value(self) -> float | None:
        """Cue's `subtitles.offsetMs`, in seconds."""
        offset = self._subtitles().get("offsetMs")
        if not isinstance(offset, int) or isinstance(offset, bool):
            return None
        return offset / 1000

    async def async_set_native_value(self, value: float) -> None:
        """Send the new delay."""
        await self.coordinator.async_command(
            "subtitle-offset", {"offsetMs": round(value * 1000)}
        )
