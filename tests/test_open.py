"""Slice D (Cue 1.2.1): play_media through /v1/open, search_media (Assist's
"play X"), browse (Continue Watching), cue.search and cue.continue_watching."""

from __future__ import annotations

import pytest

from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    DOMAIN as MP_DOMAIN,
    SERVICE_PLAY_MEDIA,
    MediaPlayerEntityFeature,
    MediaType,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import intent
from homeassistant.setup import async_setup_component

from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from .conftest import PLAYER, FakeAppleTV, make_entry, setup_entry
from .fake_cue import SERVER_ID, FakeCue


async def _play(hass: HomeAssistant, media_id: str, media_type: str = MediaType.MOVIE) -> None:
    await hass.services.async_call(
        MP_DOMAIN,
        SERVICE_PLAY_MEDIA,
        {ATTR_ENTITY_ID: PLAYER, ATTR_MEDIA_CONTENT_ID: media_id, ATTR_MEDIA_CONTENT_TYPE: media_type},
        blocking=True,
    )


def _features(hass: HomeAssistant) -> MediaPlayerEntityFeature:
    return MediaPlayerEntityFeature(hass.states.get(PLAYER).attributes["supported_features"])


# MARK: play_media


async def test_play_media_opens_in_cue(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    """No apple_tv link needed: Cue is open and serves /v1/open."""
    await setup_entry(hass, make_entry(fake_cue))
    assert {MediaPlayerEntityFeature.PLAY_MEDIA, MediaPlayerEntityFeature.SEARCH_MEDIA,
            MediaPlayerEntityFeature.BROWSE_MEDIA} <= set(_features(hass))

    await _play(hass, f"{SERVER_ID}:4321")
    assert ("POST", "/v1/open", {"ratingKey": "4321", "server": SERVER_ID}) in fake_cue.requests
    state = hass.states.get(PLAYER)
    assert state.state == "playing"
    assert state.attributes["media_content_id"] == f"{SERVER_ID}:4321"


async def test_play_show_resumes(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    """A show carries on where you are: resume, and success is whatever
    episode started."""
    await setup_entry(hass, make_entry(fake_cue))
    await _play(hass, "100", MediaType.TVSHOW)
    assert ("POST", "/v1/open", {"ratingKey": "100", "resume": True}) in fake_cue.requests
    assert hass.states.get(PLAYER).attributes["media_content_id"] == f"{SERVER_ID}:12345"


async def test_open_that_never_starts_is_an_error(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    fake_cue.open_starts = False
    await setup_entry(hass, make_entry(fake_cue))
    with pytest.raises(HomeAssistantError, match="didn't start playing"):
        await _play(hass, "4321")


async def test_open_expired_is_an_error(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    fake_cue.open_outcome = "expired"
    await setup_entry(hass, make_entry(fake_cue))
    with pytest.raises(HomeAssistantError, match="didn't start 4321: expired"):
        await _play(hass, "4321")


async def test_open_unknown_item(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    await setup_entry(hass, make_entry(fake_cue))
    with pytest.raises(HomeAssistantError, match="no such item"):
        await _play(hass, "999")


async def test_open_other_server(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    await setup_entry(hass, make_entry(fake_cue))
    with pytest.raises(HomeAssistantError, match="another Plex server"):
        await _play(hass, "otherserver:4321")


async def test_not_open_falls_back_to_apple_tv(
    hass: HomeAssistant, fake_cue: FakeCue, apple_tv: FakeAppleTV, linked_entry
) -> None:
    """Cue closed: even a 1.2.1 build is started through the apple_tv link."""
    await fake_cue.go_away()
    await hass.config_entries.async_reload(linked_entry.entry_id)
    await hass.async_block_till_done()
    await _play(hass, "100", MediaType.TVSHOW)
    assert ("play_media", "url", "cue://resume/100") in apple_tv.calls
    assert "/v1/open" not in fake_cue.paths()


async def test_not_open_and_no_apple_tv(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    entry = make_entry(fake_cue)
    await setup_entry(hass, entry)
    await fake_cue.go_away()
    await entry.runtime_data.async_refresh()  # the build is known; Cue just closed
    with pytest.raises(ServiceValidationError, match="isn't open"):
        await _play(hass, "4321")


async def test_older_build_without_apple_tv(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    """A slice C build: no open, so no play_media without a link."""
    fake_cue.before_slice_d()
    await setup_entry(hass, make_entry(fake_cue))
    features = _features(hass)
    assert MediaPlayerEntityFeature.PLAY_MEDIA not in features
    assert MediaPlayerEntityFeature.SEARCH_MEDIA not in features
    assert MediaPlayerEntityFeature.BROWSE_MEDIA not in features


# MARK: search


async def test_search_media(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    await setup_entry(hass, make_entry(fake_cue))
    response = await hass.services.async_call(
        MP_DOMAIN, "search_media", {ATTR_ENTITY_ID: PLAYER, "search_query": "grey's"},
        blocking=True, return_response=True,
    )
    results = response[PLAYER].as_dict()["result"]
    assert [r["title"] for r in results] == ["Grey's Anatomy (2005)", "Grey's Anatomy · S5 E15 · Before and After"]
    assert results[0]["media_content_id"] == f"{SERVER_ID}:100"
    assert results[0]["media_class"] == "tv_show"
    path = next(p for p in fake_cue.paths() if p == "/v1/search")
    assert path

    response = await hass.services.async_call(
        MP_DOMAIN, "search_media",
        {ATTR_ENTITY_ID: PLAYER, "search_query": "grey's", "media_filter_classes": ["episode"]},
        blocking=True, return_response=True,
    )
    assert [r["media_content_type"] for r in response[PLAYER].as_dict()["result"]] == ["episode"]

    response = await hass.services.async_call(
        MP_DOMAIN, "search_media",
        {ATTR_ENTITY_ID: PLAYER, "search_query": "grey's", "media_filter_classes": ["music"]},
        blocking=True, return_response=True,
    )
    assert response[PLAYER].as_dict()["result"] == []


async def test_search_encodes_spaces_as_percent_20(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    seen: list[str] = []
    original = fake_cue._search

    async def spy(request):
        seen.append(request.raw_path)
        return await original(request)

    fake_cue._search = spy  # type: ignore[method-assign]
    fake_cue._runner = None  # rebuild the app with the spy
    await fake_cue.go_away()
    await fake_cue.start()
    await setup_entry(hass, make_entry(fake_cue))
    await hass.services.async_call(
        "cue", "search", {ATTR_ENTITY_ID: PLAYER, "query": "the matrix"}, blocking=True, return_response=True
    )
    assert seen and "q=the%20matrix" in seen[0] and "+" not in seen[0]


async def test_assist_search_and_play(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    """Assist's HassMediaSearchAndPlay: search, then play the first result."""
    await setup_entry(hass, make_entry(fake_cue))
    assert await async_setup_component(hass, "intent", {})
    await hass.async_block_till_done()
    response = await intent.async_handle(
        hass, "test", "HassMediaSearchAndPlay",
        {"search_query": {"value": "matrix"}, "name": {"value": "Living Room"}},
    )
    assert response.response_type == intent.IntentResponseType.ACTION_DONE
    assert ("POST", "/v1/open", {"ratingKey": "4321", "server": SERVER_ID}) in fake_cue.requests
    assert hass.states.get(PLAYER).attributes["media_title"] == "The Matrix"


async def test_cue_search_service(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    await setup_entry(hass, make_entry(fake_cue))
    response = await hass.services.async_call(
        "cue", "search", {ATTR_ENTITY_ID: PLAYER, "query": "grey", "type": ["show"], "limit": 5},
        blocking=True, return_response=True,
    )
    result = response[PLAYER]
    assert result["query"] == "grey"
    assert [r["ratingKey"] for r in result["results"]] == ["100"]
    assert result["results"][0]["media_content_id"] == f"{SERVER_ID}:100"
    assert result["results"][0]["media_content_type"] == "tvshow"
    assert result["results"][0]["display_title"] == "Grey's Anatomy (2005)"


async def test_continue_watching_service(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    await setup_entry(hass, make_entry(fake_cue))
    response = await hass.services.async_call(
        "cue", "continue_watching", {ATTR_ENTITY_ID: PLAYER}, blocking=True, return_response=True
    )
    items = response[PLAYER]["items"]
    assert [i["ratingKey"] for i in items] == ["12345", "4321"]
    assert items[0]["viewOffsetMs"] == 1155000
    assert items[0]["display_title"] == "Grey's Anatomy · S5 E15 · Before and After"


async def test_services_need_a_newer_cue(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    fake_cue.before_slice_d()
    await setup_entry(hass, make_entry(fake_cue))
    with pytest.raises(ServiceValidationError, match="newer Cue"):
        await hass.services.async_call(
            "cue", "search", {ATTR_ENTITY_ID: PLAYER, "query": "x"}, blocking=True, return_response=True
        )


async def test_signed_out(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    fake_cue.signed_in = False
    await setup_entry(hass, make_entry(fake_cue))
    with pytest.raises(HomeAssistantError, match="isn't signed in"):
        await hass.services.async_call(
            "cue", "continue_watching", {ATTR_ENTITY_ID: PLAYER}, blocking=True, return_response=True
        )


# MARK: browse


async def test_browse_continue_watching(
    hass: HomeAssistant, fake_cue: FakeCue, hass_ws_client: WebSocketGenerator
) -> None:
    await setup_entry(hass, make_entry(fake_cue))
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "media_player/browse_media", "entity_id": PLAYER})
    root = (await client.receive_json())["result"]
    assert [c["title"] for c in root["children"]] == ["Continue Watching"]

    await client.send_json({
        "id": 2, "type": "media_player/browse_media", "entity_id": PLAYER,
        "media_content_id": "cue_continue", "media_content_type": "video",
    })
    folder = (await client.receive_json())["result"]
    assert [c["media_content_id"] for c in folder["children"]] == [f"{SERVER_ID}:12345", f"{SERVER_ID}:4321"]
    assert all(c["can_play"] for c in folder["children"])
