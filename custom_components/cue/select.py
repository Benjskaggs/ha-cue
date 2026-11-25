"""Subtitle, audio and speed selects."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, SPEEDS, SUBTITLES_OFF
from .coordinator import CueConfigEntry, CueCoordinator
from .entity import CueEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CueConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the selects."""
    coordinator = entry.runtime_data
    entities: list[SelectEntity] = []
    if coordinator.has("subtitles"):
        entities.append(CueTrackSelect(coordinator, "subtitles", allow_off=True))
    if coordinator.has("audio"):
        entities.append(CueTrackSelect(coordinator, "audio", allow_off=False))
    if coordinator.has("rate"):
        entities.append(CueSpeedSelect(coordinator))
    async_add_entities(entities)


def track_options(tracks: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """(label, id) per track. Titles repeat (two "English (SRT)"), so a
    repeated title gets its position appended to stay selectable."""
    titles = [str(t.get("title") or t.get("id")) for t in tracks]
    seen: dict[str, int] = {}
    out: list[tuple[str, str]] = []
    for track, title in zip(tracks, titles, strict=True):
        if titles.count(title) > 1:
            seen[title] = seen.get(title, 0) + 1
            title = f"{title} ({seen[title]})"
        out.append((title, str(track.get("id"))))
    return out


class CueTrackSelect(CueEntity, SelectEntity):
    """Subtitles (with Off) or audio, by track."""

    def __init__(self, coordinator: CueCoordinator, kind: str, *, allow_off: bool) -> None:
        super().__init__(coordinator, kind)
        self._kind = kind
        self._allow_off = allow_off
        self._attr_translation_key = kind

    def _block(self) -> dict[str, Any]:
        player = self.player
        block = (player or {}).get(self._kind)
        return block if isinstance(block, dict) else {}

    def _pairs(self) -> list[tuple[str, str]]:
        tracks = self._block().get("tracks")
        return track_options(tracks) if isinstance(tracks, list) else []

    @property
    def available(self) -> bool:
        """Only while something is playing (and has tracks, for audio)."""
        if not super().available or self.player is None:
            return False
        return self._allow_off or bool(self._pairs())

    @property
    def options(self) -> list[str]:
        """Track titles, plus Off for subtitles."""
        labels = [label for label, _ in self._pairs()]
        return [SUBTITLES_OFF, *labels] if self._allow_off else labels

    @property
    def current_option(self) -> str | None:
        """The selected track's label."""
        selected = self._block().get("selected")
        if selected is None:
            return SUBTITLES_OFF if self._allow_off and self.player else None
        for label, track_id in self._pairs():
            if track_id == str(selected):
                return label
        return None

    async def async_select_option(self, option: str) -> None:
        """Send the track id back to Cue."""
        if self._allow_off and option == SUBTITLES_OFF:
            await self.coordinator.async_command(self._kind, {"id": None})
            return
        for label, track_id in self._pairs():
            if label == option:
                await self.coordinator.async_command(self._kind, {"id": track_id})
                return
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_option",
            translation_placeholders={"option": option},
        )


def speed_label(rate: float) -> str:
    """`1.25×`, `1×`."""
    return f"{rate:g}×"


class CueSpeedSelect(CueEntity, SelectEntity):
    """Playback speed for this item (Cue doesn't save it)."""

    _attr_translation_key = "speed"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: CueCoordinator) -> None:
        super().__init__(coordinator, "speed")

    @property
    def available(self) -> bool:
        """Only while something is playing."""
        return super().available and self.player is not None

    def _rate(self) -> float | None:
        rate = (self.player or {}).get("rate")
        return float(rate) if isinstance(rate, (int, float)) else None

    @property
    def options(self) -> list[str]:
        """The fixed speeds, plus the current one if Cue is at another."""
        rates = list(SPEEDS)
        rate = self._rate()
        # Cue reports rate 0 while paused on some builds; that's not a speed.
        if rate and not any(abs(rate - r) < 0.01 for r in rates):
            rates.append(round(rate, 2))
            rates.sort()
        return [speed_label(r) for r in rates]

    @property
    def current_option(self) -> str | None:
        """The current speed."""
        rate = self._rate()
        if not rate:
            return None
        for r in SPEEDS:
            if abs(rate - r) < 0.01:
                return speed_label(r)
        return speed_label(round(rate, 2))

    async def async_select_option(self, option: str) -> None:
        """Set the speed."""
        try:
            rate = float(option.rstrip("×x").strip())
        except ValueError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_option",
                translation_placeholders={"option": option},
            ) from err
        await self.coordinator.async_command("rate", {"rate": rate})
