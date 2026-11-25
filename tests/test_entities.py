"""The media player, selects, buttons and sensor while something plays."""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.components.media_player import (
    ATTR_MEDIA_SEEK_POSITION,
    DOMAIN as MP_DOMAIN,
    SERVICE_MEDIA_NEXT_TRACK,
    SERVICE_MEDIA_PAUSE,
    SERVICE_MEDIA_PLAY,
    SERVICE_MEDIA_SEEK,
    MediaPlayerEntityFeature,
)
from homeassistant.components.select import ATTR_OPTION, SERVICE_SELECT_OPTION
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.cue.diagnostics import async_get_config_entry_diagnostics

from .conftest import PLAYER, make_entry, setup_entry
from .fake_cue import JPEG, TOKEN, FakeCue

SUBS = "select.living_room_subtitles"
AUDIO = "select.living_room_audio_track"
SPEED = "select.living_room_speed"
SKIP_INTRO = "button.living_room_skip_intro"
SKIP_CREDITS = "button.living_room_skip_credits"
RESTART = "button.living_room_restart"


@pytest.fixture
async def playing(hass: HomeAssistant, fake_cue: FakeCue) -> MockConfigEntry:
    """An entry whose first poll finds an episode playing."""
    fake_cue.play()
    entry = make_entry(fake_cue)
    await setup_entry(hass, entry)
    return entry


async def _mp(hass: HomeAssistant, service: str, **data) -> None:
    await hass.services.async_call(
        MP_DOMAIN, service, {ATTR_ENTITY_ID: PLAYER, **data}, blocking=True
    )


async def _select(hass: HomeAssistant, entity_id: str, option: str) -> None:
    await hass.services.async_call(
        "select", SERVICE_SELECT_OPTION, {ATTR_ENTITY_ID: entity_id, ATTR_OPTION: option}, blocking=True
    )


async def _press(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: entity_id}, blocking=True)


async def test_playing_attributes(hass: HomeAssistant, fake_cue: FakeCue, playing) -> None:
    """Everything scout §3 maps from status."""
    state = hass.states.get(PLAYER)
    assert state.state == "playing"
    a = state.attributes
    assert a["media_content_id"] == "0123abcd:12345"
    assert a["media_content_type"] == "episode"
    assert a["media_title"] == "Before and After"
    assert a["media_series_title"] == "Grey's Anatomy"
    assert a["media_season"] == "5"
    assert a["media_episode"] == "15"
    assert a["media_duration"] == 2583.0
    assert a["media_position"] == 1155.0
    sent_at = dt_util.parse_datetime(fake_cue.player["positionAt"])
    assert a["media_position_updated_at"] == sent_at
    assert a["app_name"] == "Cue"
    assert a["rate"] == 1.0
    assert a["next_title"] == "Rise Up"
    assert a["entity_picture"].startswith("/api/media_player_proxy/media_player.living_room?")
    features = MediaPlayerEntityFeature(a["supported_features"])
    assert MediaPlayerEntityFeature.NEXT_TRACK in features
    assert MediaPlayerEntityFeature.SEEK in features
    # No linked apple_tv entity: no play_media.
    assert MediaPlayerEntityFeature.PLAY_MEDIA not in features
    assert MediaPlayerEntityFeature.VOLUME_SET not in features

    assert hass.states.get(SUBS).state == "English (SRT)"
    assert hass.states.get(SUBS).attributes["options"] == ["Off", "English (SRT)", "Spanish (SRT)"]
    assert hass.states.get(AUDIO).state == "English 5.1"
    assert hass.states.get(SPEED).state == "1×"
    assert hass.states.get(RESTART).state != STATE_UNAVAILABLE
    assert hass.states.get(SKIP_INTRO).state == STATE_UNAVAILABLE
    assert hass.states.get(SKIP_CREDITS).state == STATE_UNAVAILABLE


async def test_transport_commands(hass: HomeAssistant, fake_cue: FakeCue, playing) -> None:
    """play, pause, seek, next: each reply is applied without waiting for a poll."""
    await _mp(hass, SERVICE_MEDIA_PAUSE)
    assert hass.states.get(PLAYER).state == "paused"
    await _mp(hass, SERVICE_MEDIA_PLAY)
    assert hass.states.get(PLAYER).state == "playing"

    await _mp(hass, SERVICE_MEDIA_SEEK, **{ATTR_MEDIA_SEEK_POSITION: 600.4})
    assert fake_cue.requests[-1] == ("POST", "/v1/player/seek", {"positionMs": 600400})
    assert hass.states.get(PLAYER).attributes["media_position"] == 600.4

    await _mp(hass, SERVICE_MEDIA_NEXT_TRACK)
    state = hass.states.get(PLAYER)
    assert state.attributes["media_content_id"] == "0123abcd:12346"
    assert state.attributes["media_title"] == "Rise Up"
    # The last episode: NEXT_TRACK goes away.
    assert MediaPlayerEntityFeature.NEXT_TRACK not in MediaPlayerEntityFeature(
        state.attributes["supported_features"]
    )

    posts = [p for p in fake_cue.paths("POST")]
    assert posts == [
        "/v1/player/pause",
        "/v1/player/play",
        "/v1/player/seek",
        "/v1/player/next",
    ]


async def test_command_error_surfaces(hass: HomeAssistant, fake_cue: FakeCue, playing) -> None:
    """A Cue error is a HomeAssistantError carrying Cue's code; nothing is applied."""
    fake_cue.idle()  # the episode ended between polls
    with pytest.raises(HomeAssistantError, match="Nothing is playing"):
        await _mp(hass, SERVICE_MEDIA_PAUSE)
    with pytest.raises(HomeAssistantError, match="Nothing is playing"):
        await _press(hass, RESTART)
    # The error reply wasn't applied as status; the next poll goes idle.
    assert hass.states.get(PLAYER).state == "playing"
    await playing.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(PLAYER).state == "idle"


async def test_command_when_cue_closed(hass: HomeAssistant, fake_cue: FakeCue, playing) -> None:
    """Cue suspended between poll and command: error, and the player goes off."""
    await fake_cue.go_away()
    with pytest.raises(HomeAssistantError, match="isn't open"):
        await _mp(hass, SERVICE_MEDIA_PAUSE)
    assert hass.states.get(PLAYER).state == STATE_OFF


async def test_command_401_starts_reauth(
    freezer: FrozenDateTimeFactory, hass: HomeAssistant, fake_cue: FakeCue, playing
) -> None:
    """A 401 on a command stops everything until reauth."""
    fake_cue.token = "REGENERATED"
    with pytest.raises(HomeAssistantError, match="Re-authenticate"):
        await _mp(hass, SERVICE_MEDIA_PAUSE)
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler("cue")
    assert [f["context"]["source"] for f in flows] == ["reauth"]

    before = len(fake_cue.requests)
    with pytest.raises(HomeAssistantError):
        await _mp(hass, SERVICE_MEDIA_PLAY)
    for _ in range(10):
        freezer.tick(timedelta(seconds=30))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert len(fake_cue.requests) == before
    assert fake_cue.failed_auth == 1


async def test_track_selects(hass: HomeAssistant, fake_cue: FakeCue, playing) -> None:
    """Subtitles (including Off) and audio by track id."""
    await _select(hass, SUBS, "Spanish (SRT)")
    assert fake_cue.requests[-1] == ("POST", "/v1/player/subtitles", {"id": "4"})
    assert hass.states.get(SUBS).state == "Spanish (SRT)"

    await _select(hass, SUBS, "Off")
    assert fake_cue.requests[-1] == ("POST", "/v1/player/subtitles", {"id": None})
    assert hass.states.get(SUBS).state == "Off"

    await _select(hass, AUDIO, "Commentary")
    assert fake_cue.requests[-1] == ("POST", "/v1/player/audio", {"id": "2"})
    assert hass.states.get(AUDIO).state == "Commentary"


async def test_duplicate_track_titles(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    """Two tracks with one title both stay selectable."""
    fake_cue.play()
    fake_cue.player["subtitles"]["tracks"][1]["title"] = "English (SRT)"
    await setup_entry(hass, make_entry(fake_cue))
    assert hass.states.get(SUBS).attributes["options"] == [
        "Off",
        "English (SRT) (1)",
        "English (SRT) (2)",
    ]
    await _select(hass, SUBS, "English (SRT) (2)")
    assert fake_cue.requests[-1] == ("POST", "/v1/player/subtitles", {"id": "4"})


async def test_speed(hass: HomeAssistant, fake_cue: FakeCue, playing) -> None:
    """Speed sets the rate; an off-list rate from the TV still shows."""
    await _select(hass, SPEED, "1.5×")
    assert fake_cue.requests[-1] == ("POST", "/v1/player/rate", {"rate": 1.5})
    assert hass.states.get(SPEED).state == "1.5×"
    assert hass.states.get(PLAYER).attributes["rate"] == 1.5

    fake_cue.player["rate"] = 1.1
    await hass.config_entries.async_reload(playing.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get(SPEED)
    assert state.state == "1.1×"
    assert "1.1×" in state.attributes["options"]


async def test_skip_buttons(hass: HomeAssistant, fake_cue: FakeCue, playing) -> None:
    """Skip Intro / Credits only while Cue says the marker is skippable."""
    fake_cue.set_marker("intro", skippable=True)
    await playing.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(SKIP_INTRO).state != STATE_UNAVAILABLE
    assert hass.states.get(SKIP_CREDITS).state == STATE_UNAVAILABLE

    await _press(hass, SKIP_INTRO)
    assert fake_cue.requests[-1] == ("POST", "/v1/player/skip", {"marker": "intro"})
    assert hass.states.get(PLAYER).attributes["media_position"] == 95.0
    assert hass.states.get(SKIP_INTRO).state == STATE_UNAVAILABLE

    fake_cue.set_marker("credits", skippable=True)
    await playing.runtime_data.async_refresh()
    await hass.async_block_till_done()
    await _press(hass, SKIP_CREDITS)
    assert fake_cue.requests[-1] == ("POST", "/v1/player/skip", {"marker": "credits"})


async def test_restart(hass: HomeAssistant, fake_cue: FakeCue, playing) -> None:
    """Restart seeks to 0."""
    await _press(hass, RESTART)
    assert fake_cue.requests[-1][1] == "/v1/player/restart"
    assert hass.states.get(PLAYER).attributes["media_position"] == 0


async def test_artwork_proxied_with_token(
    hass: HomeAssistant,
    fake_cue: FakeCue,
    playing,
    hass_client: ClientSessionGenerator,
) -> None:
    """HA's image proxy fetches /v1/artwork/still with the bearer token."""
    client = await hass_client()
    url = hass.states.get(PLAYER).attributes["entity_picture"]
    resp = await client.get(url)
    assert resp.status == HTTPStatus.OK
    assert await resp.read() == JPEG
    assert fake_cue.paths("GET")[-1] == "/v1/artwork/still"


async def test_diagnostics_redact_token(
    hass: HomeAssistant, fake_cue: FakeCue, playing
) -> None:
    """The token never appears in diagnostics."""
    diag = await async_get_config_entry_diagnostics(hass, playing)
    assert diag["entry"]["data"]["token"] == "**REDACTED**"
    assert TOKEN not in repr(diag)
    assert diag["reachable"] is True
    assert diag["status"]["player"]["item"]["ratingKey"] == "12345"
