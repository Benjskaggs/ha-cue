"""Polling, `off` when Cue isn't open, and never retrying a 401."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.cue.const import DOMAIN

from .conftest import PLAYER, make_entry, setup_entry
from .fake_cue import FakeCue

OPEN = "binary_sensor.living_room_cue_open"


async def _tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float = 6) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    # The coordinator polls in a background task.
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_idle(hass: HomeAssistant, fake_cue: FakeCue, entry: MockConfigEntry) -> None:
    """Cue open, nothing playing."""
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(PLAYER).state == "idle"
    assert hass.states.get(OPEN).state == STATE_ON
    for entity_id in (
        "select.living_room_subtitles",
        "select.living_room_audio_track",
        "select.living_room_speed",
        "button.living_room_skip_intro",
        "button.living_room_skip_credits",
        "button.living_room_restart",
    ):
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE, entity_id


async def test_unreachable_is_off(
    freezer: FrozenDateTimeFactory, hass: HomeAssistant, fake_cue: FakeCue
) -> None:
    """Cue not open at setup: the entry loads, the player is off; it comes back."""
    await fake_cue.go_away()
    entry = make_entry(fake_cue)
    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(PLAYER).state == STATE_OFF
    assert hass.states.get(OPEN).state == STATE_OFF

    await fake_cue.come_back()
    fake_cue.play()
    await _tick(hass, freezer)
    assert hass.states.get(PLAYER).state == "playing"
    assert hass.states.get(OPEN).state == STATE_ON

    # Suspended mid-episode: off again, not unavailable.
    await fake_cue.go_away()
    await _tick(hass, freezer)
    assert hass.states.get(PLAYER).state == STATE_OFF
    assert hass.states.get(OPEN).state == STATE_OFF


async def test_401_at_setup_never_retried(
    freezer: FrozenDateTimeFactory, hass: HomeAssistant, fake_cue: FakeCue
) -> None:
    """One 401, reauth starts, and nothing more reaches the Apple TV."""
    entry = make_entry(fake_cue, token="WRONG")
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert fake_cue.failed_auth == 1

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]

    for _ in range(20):
        await _tick(hass, freezer, 30)
    assert fake_cue.failed_auth == 1
    assert fake_cue.locked_retry_after is None


async def test_401_mid_run_stops_polling(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    fake_cue: FakeCue,
    entry: MockConfigEntry,
) -> None:
    """The token regenerated on the TV while HA ran: stop and reauth."""
    fake_cue.token = "NEW-TOKEN-ON-THE-TV"
    await _tick(hass, freezer)
    assert fake_cue.failed_auth == 1

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]
    assert hass.states.get(PLAYER).state == STATE_UNAVAILABLE

    polls = fake_cue.count("/v1/status")
    for _ in range(20):
        await _tick(hass, freezer, 30)
    assert fake_cue.count("/v1/status") == polls
    assert fake_cue.failed_auth == 1


async def test_rate_limited_backs_off(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    fake_cue: FakeCue,
    entry: MockConfigEntry,
) -> None:
    """429: unavailable, and silent until Retry-After passes."""
    fake_cue.locked_retry_after = 60
    await _tick(hass, freezer)
    assert hass.states.get(PLAYER).state == STATE_UNAVAILABLE
    polls = fake_cue.count("/v1/status")

    for _ in range(8):
        await _tick(hass, freezer)  # 48 s: still inside the lockout
    assert fake_cue.count("/v1/status") == polls

    fake_cue.locked_retry_after = None
    fake_cue.play()
    await _tick(hass, freezer, 20)
    assert fake_cue.count("/v1/status") == polls + 1
    assert hass.states.get(PLAYER).state == "playing"


async def test_disabled_is_unavailable(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    fake_cue: FakeCue,
    entry: MockConfigEntry,
) -> None:
    """Control turned off on the TV: unavailable (Cue answers, but won't serve)."""
    fake_cue.disabled = True
    await _tick(hass, freezer)
    assert hass.states.get(PLAYER).state == STATE_UNAVAILABLE

    fake_cue.disabled = False
    await _tick(hass, freezer)
    assert hass.states.get(PLAYER).state == "idle"


async def test_scan_interval_option(
    freezer: FrozenDateTimeFactory, hass: HomeAssistant, fake_cue: FakeCue
) -> None:
    """The poll follows the option."""
    entry = make_entry(fake_cue, options={"scan_interval": 10})
    await setup_entry(hass, entry)
    polls = fake_cue.count("/v1/status")
    await _tick(hass, freezer, 6)
    assert fake_cue.count("/v1/status") == polls
    await _tick(hass, freezer, 5)
    assert fake_cue.count("/v1/status") == polls + 1


async def test_unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Unload cleanly."""
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
