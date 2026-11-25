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
    (or was very recently: tvOS suspends it soon after it leaves). Builds
    with `status.app` also say when they're on their way out
    (`app.foreground: false`), which turns it off a little sooner."""

    _attr_translation_key = "cue_open"

    def __init__(self, coordinator: CueCoordinator) -> None:
        super().__init__(coordinator, "cue_open")

    @property
    def is_on(self) -> bool:
        """Reachable, and not saying it has left the foreground."""
        data = self.coordinator.data
        if not (data and data.reachable):
            return False
        app = (data.status or {}).get("app")
        return not (isinstance(app, dict) and app.get("foreground") is False)

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """`screen`: what Cue shows (`player`, `home`, …), on builds that say."""
        data = self.coordinator.data
        app = (data.status or {}).get("app") if data and data.reachable else None
        screen = app.get("screen") if isinstance(app, dict) else None
        return {"screen": screen} if isinstance(screen, str) else None
