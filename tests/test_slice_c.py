"""Slice C (Cue 1.2.1): stop, previous, the subtitle timing number, and
`status.app` on the "Cue open" sensor. All feature-detected."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.components.media_player import (
    DOMAIN as MP_DOMAIN,
    SERVICE_MEDIA_PREVIOUS_TRACK,
    SERVICE_MEDIA_STOP,
    MediaPlayerEntityFeature,
)
from homeassistant.components.number import ATTR_VALUE, DOMAIN as NUMBER_DOMAIN, SERVICE_SET_VALUE
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from pytest_homeassistant_custom_component.common import async_fire_time_changed

from .conftest import PLAYER, make_entry, setup_entry
from .fake_cue import FakeCue

OFFSET = "number.living_room_subtitle_timing"
OPEN = "binary_sensor.living_room_cue_open"


def _features(hass: HomeAssistant) -> MediaPlayerEntityFeature:
    return MediaPlayerEntityFeature(hass.states.get(PLAYER).attributes["supported_features"])


async def _start(hass: HomeAssistant, fake: FakeCue) -> None:
    fake.play()
    await setup_entry(hass, make_entry(fake))


async def test_stop_and_previous(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    await _start(hass, fake_cue)
    assert {MediaPlayerEntityFeature.STOP, MediaPlayerEntityFeature.PREVIOUS_TRACK} <= set(_features(hass))

    await hass.services.async_call(MP_DOMAIN, SERVICE_MEDIA_PREVIOUS_TRACK, {ATTR_ENTITY_ID: PLAYER}, blocking=True)
    assert fake_cue.requests[-1][1] == "/v1/player/previous"
    state = hass.states.get(PLAYER)
    assert state.attributes["media_episode"] == "14"
    # The first episode: PREVIOUS_TRACK goes away.
    assert MediaPlayerEntityFeature.PREVIOUS_TRACK not in _features(hass)

    await hass.services.async_call(MP_DOMAIN, SERVICE_MEDIA_STOP, {ATTR_ENTITY_ID: PLAYER}, blocking=True)
    assert fake_cue.requests[-1][1] == "/v1/player/stop"
    assert hass.states.get(PLAYER).state == "idle"


async def test_older_build_has_no_stop_previous_or_offset(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    """A 1.2 build: queue.previous is there, but no route to use it."""
    fake_cue.before_slice_c()
    await _start(hass, fake_cue)
    features = _features(hass)
    assert MediaPlayerEntityFeature.STOP not in features
    assert MediaPlayerEntityFeature.PREVIOUS_TRACK not in features
    assert hass.states.get(OFFSET) is None
    assert hass.states.get(OPEN).state == STATE_ON
    assert "screen" not in hass.states.get(OPEN).attributes


async def test_subtitle_offset(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    await _start(hass, fake_cue)
    assert float(hass.states.get(OFFSET).state) == 0.0

    await hass.services.async_call(
        NUMBER_DOMAIN, SERVICE_SET_VALUE, {ATTR_ENTITY_ID: OFFSET, ATTR_VALUE: -1.25}, blocking=True
    )
    assert fake_cue.requests[-1] == ("POST", "/v1/player/subtitle-offset", {"offsetMs": -1250})
    assert float(hass.states.get(OFFSET).state) == -1.25

    # Out of range is refused by HA before Cue sees it.
    with pytest.raises((HomeAssistantError, ValueError)):
        await hass.services.async_call(
            NUMBER_DOMAIN, SERVICE_SET_VALUE, {ATTR_ENTITY_ID: OFFSET, ATTR_VALUE: 30}, blocking=True
        )
    assert fake_cue.requests[-1][2] == {"offsetMs": -1250}


async def test_subtitle_offset_unavailable_when_subtitles_off(
    hass: HomeAssistant, fake_cue: FakeCue
) -> None:
    fake_cue.play()
    fake_cue.player["subtitles"]["selected"] = None
    await setup_entry(hass, make_entry(fake_cue))
    assert hass.states.get(OFFSET).state == STATE_UNAVAILABLE


async def test_subtitle_offset_idle(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    await setup_entry(hass, make_entry(fake_cue))
    assert hass.states.get(OFFSET).state == STATE_UNAVAILABLE


async def test_foreground_lost_turns_sensor_off(
    freezer: FrozenDateTimeFactory, hass: HomeAssistant, fake_cue: FakeCue
) -> None:
    await _start(hass, fake_cue)
    assert hass.states.get(OPEN).state == STATE_ON
    assert hass.states.get(OPEN).attributes["screen"] == "player"

    fake_cue.foreground = False
    freezer.tick(timedelta(seconds=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(OPEN).state == STATE_OFF

    await fake_cue.go_away()
    freezer.tick(timedelta(seconds=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(OPEN).state == STATE_OFF
    assert "screen" not in hass.states.get(OPEN).attributes


async def test_stop_mid_hop_is_success(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    """Live test 2026-11-25: a stop that lands between episodes got 409
    nothing_playing, which surfaced as a 500. Nothing playing is what stop
    asks for."""
    await _start(hass, fake_cue)
    fake_cue.idle()  # the hop: the old player's gone, the next isn't up yet
    await hass.services.async_call(MP_DOMAIN, SERVICE_MEDIA_STOP, {ATTR_ENTITY_ID: PLAYER}, blocking=True)
    assert hass.states.get(PLAYER).state == "idle"
