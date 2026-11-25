"""`play_media` cold start through the linked apple_tv entity."""

from __future__ import annotations

import pytest

from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    ATTR_MEDIA_EXTRA,
    DOMAIN as MP_DOMAIN,
    SERVICE_PLAY_MEDIA,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.cue.media_player import cue_play_url, parse_media_id

from .conftest import PLAYER, FakeAppleTV, make_entry, setup_entry
from .fake_cue import FakeCue


async def _play(hass: HomeAssistant, media_id: str, extra: dict | None = None) -> None:
    data = {
        ATTR_ENTITY_ID: PLAYER,
        ATTR_MEDIA_CONTENT_TYPE: "episode",
        ATTR_MEDIA_CONTENT_ID: media_id,
    }
    if extra is not None:
        data[ATTR_MEDIA_EXTRA] = extra
    await hass.services.async_call(MP_DOMAIN, SERVICE_PLAY_MEDIA, data, blocking=True)


def test_parse_media_id() -> None:
    """Bare keys, server-qualified keys (what the entity reports), extra.server."""
    assert parse_media_id("12345", None) == ("12345", None)
    assert parse_media_id("0123abcd:12345", None) == ("12345", "0123abcd")
    assert parse_media_id("12345", {"server": "0123abcd"}) == ("12345", "0123abcd")
    for bad in ("", "abc", "12/34", "cue://play/1", "bad server:1"):
        with pytest.raises(ServiceValidationError):
            parse_media_id(bad, None)
    assert cue_play_url("12345", None) == "cue://play/12345"
    assert cue_play_url("12345", "0123abcd") == "cue://play/12345?server=0123abcd"



@pytest.fixture(autouse=True)
def cold_start_build(fake_cue: FakeCue) -> None:
    """These tests cover the apple_tv cold start, the only path before Cue
    1.2.1 (and still the one when Cue isn't open): a build without
    `/v1/open`. `test_open.py` covers the open path."""
    fake_cue.before_slice_d()

async def test_cold_start_from_closed(
    hass: HomeAssistant, fake_cue: FakeCue, apple_tv: FakeAppleTV
) -> None:
    """Cue not open: select_source Cue, wait for app_name, then the link."""
    await fake_cue.go_away()
    entry = make_entry(fake_cue, options={"apple_tv_entity": apple_tv.entity_id})
    await setup_entry(hass, entry)
    state = hass.states.get(PLAYER)
    assert state.state == MediaPlayerState.OFF
    assert MediaPlayerEntityFeature.PLAY_MEDIA in MediaPlayerEntityFeature(
        state.attributes["supported_features"]
    )

    await _play(hass, "0123abcd:12345")

    assert apple_tv.calls == [
        ("select_source", "Cue"),
        ("play_media", "url", "cue://play/12345?server=0123abcd"),
    ]
    state = hass.states.get(PLAYER)
    assert state.state == MediaPlayerState.PLAYING
    assert state.attributes["media_content_id"] == "0123abcd:12345"


async def test_cue_already_frontmost(
    hass: HomeAssistant, fake_cue: FakeCue, apple_tv: FakeAppleTV, linked_entry
) -> None:
    """Cue in front already: straight to the link."""
    apple_tv._attr_app_name = "Cue"
    apple_tv.async_write_ha_state()
    await _play(hass, "777")
    assert apple_tv.calls == [("play_media", "url", "cue://play/777")]
    assert hass.states.get(PLAYER).attributes["media_content_id"] == "0123abcd:777"


async def test_wakes_a_sleeping_apple_tv(
    hass: HomeAssistant, fake_cue: FakeCue, apple_tv: FakeAppleTV, linked_entry
) -> None:
    """Standby first wakes the TV."""
    apple_tv._attr_state = MediaPlayerState.STANDBY
    apple_tv.async_write_ha_state()
    await _play(hass, "12345")
    assert [c[0] for c in apple_tv.calls] == ["turn_on", "select_source", "play_media"]
    assert hass.states.get(PLAYER).state == MediaPlayerState.PLAYING


async def test_wake_fails(
    hass: HomeAssistant, fake_cue: FakeCue, apple_tv: FakeAppleTV, linked_entry
) -> None:
    """The TV doesn't wake: an error, and no link is sent."""
    apple_tv._attr_state = MediaPlayerState.OFF
    apple_tv.wakes = False
    apple_tv.async_write_ha_state()
    with pytest.raises(HomeAssistantError, match="didn't wake"):
        await _play(hass, "12345")
    assert [c[0] for c in apple_tv.calls] == ["turn_on"]


async def test_launch_fails(
    hass: HomeAssistant, fake_cue: FakeCue, apple_tv: FakeAppleTV, linked_entry
) -> None:
    """Cue never comes to the front: an error, and no link (which would prompt)."""
    apple_tv.launches = False
    with pytest.raises(HomeAssistantError, match="didn't come to the front"):
        await _play(hass, "12345")
    assert apple_tv.calls == [("select_source", "Cue")]


async def test_link_sent_but_nothing_plays(
    hass: HomeAssistant, fake_cue: FakeCue, apple_tv: FakeAppleTV, linked_entry
) -> None:
    """A server mismatch shows the page only: that's a failure, not success."""
    apple_tv.link_plays = False
    with pytest.raises(HomeAssistantError, match="didn't start playing"):
        await _play(hass, "12345", {"server": "someotherserver"})
    assert apple_tv.calls[-1] == ("play_media", "url", "cue://play/12345?server=someotherserver")
    assert hass.states.get(PLAYER).state == MediaPlayerState.IDLE


async def test_different_item_playing_is_not_success(
    hass: HomeAssistant, fake_cue: FakeCue, apple_tv: FakeAppleTV, linked_entry
) -> None:
    """Something else keeps playing: not the item asked for, so it fails."""
    fake_cue.play("999")
    await linked_entry.runtime_data.async_refresh()
    apple_tv._attr_app_name = "Cue"
    apple_tv.link_plays = False
    apple_tv.async_write_ha_state()
    with pytest.raises(HomeAssistantError, match="didn't start playing"):
        await _play(hass, "12345")


async def test_no_linked_apple_tv(hass: HomeAssistant, fake_cue: FakeCue, entry) -> None:
    """Without a linked entity, PLAY_MEDIA isn't offered at all."""
    state = hass.states.get(PLAYER)
    assert MediaPlayerEntityFeature.PLAY_MEDIA not in MediaPlayerEntityFeature(
        state.attributes["supported_features"]
    )
    with pytest.raises(HomeAssistantError):
        await _play(hass, "12345")


async def test_linked_entity_missing(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    """A linked entity that's gone is a clear error."""
    entry = make_entry(fake_cue, options={"apple_tv_entity": "media_player.gone"})
    await setup_entry(hass, entry)
    with pytest.raises(HomeAssistantError, match="doesn't exist"):
        await _play(hass, "12345")
