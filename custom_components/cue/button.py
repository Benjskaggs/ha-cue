"""Skip Intro, Skip Credits and Restart."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import CueConfigEntry, CueCoordinator
from .entity import CueEntity

PARALLEL_UPDATES = 0


def marker_skippable(kind: str) -> Callable[[dict[str, Any]], bool]:
    """True while Cue says the `kind` marker can be skipped right now."""

    def check(player: dict[str, Any]) -> bool:
        return any(
            isinstance(m, dict) and m.get("type") == kind and m.get("skippable") is True
            for m in player.get("markers") or []
        )

    return check


@dataclass(frozen=True, kw_only=True)
class CueButtonDescription(ButtonEntityDescription):
    """A button that sends one player command."""

    command: str
    body: dict[str, Any] | None = None
    available_fn: Callable[[dict[str, Any]], bool] = lambda player: True


BUTTONS: tuple[CueButtonDescription, ...] = (
    CueButtonDescription(
        key="skip_intro",
        translation_key="skip_intro",
        command="skip",
        body={"marker": "intro"},
        available_fn=marker_skippable("intro"),
    ),
    CueButtonDescription(
        key="skip_credits",
        translation_key="skip_credits",
        command="skip",
        body={"marker": "credits"},
        available_fn=marker_skippable("credits"),
    ),
    CueButtonDescription(
        key="restart",
        translation_key="restart",
        command="restart",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CueConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the buttons."""
    coordinator = entry.runtime_data
    async_add_entities(
        CueButton(coordinator, description)
        for description in BUTTONS
        if coordinator.has(description.command)
    )


class CueButton(CueEntity, ButtonEntity):
    """One player command."""

    entity_description: CueButtonDescription

    def __init__(self, coordinator: CueCoordinator, description: CueButtonDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Skips only while their marker is skippable; Restart while playing."""
        player = self.player
        return (
            super().available
            and player is not None
            and self.entity_description.available_fn(player)
        )

    async def async_press(self) -> None:
        """Send the command."""
        await self.coordinator.async_command(
            self.entity_description.command, self.entity_description.body
        )
