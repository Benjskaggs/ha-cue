"""Slice G (Cue 1.2.1): cue.cast, cue.on_screen, cue.filmography, and the
headshot through HA's image proxy."""

from __future__ import annotations

from http import HTTPStatus

import pytest

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.cue import media_player as cue_media_player

from .conftest import PLAYER, make_entry, setup_entry
from .fake_cue import JPEG, SERVER_ID, FakeCue


async def _call(hass: HomeAssistant, service: str, **data):
    response = await hass.services.async_call(
        "cue", service, {ATTR_ENTITY_ID: PLAYER, **data}, blocking=True, return_response=True
    )
    return response[PLAYER]


@pytest.fixture
async def playing(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    fake_cue.play()
    await setup_entry(hass, make_entry(fake_cue))


async def test_cast(hass: HomeAssistant, fake_cue: FakeCue, playing, hass_client: ClientSessionGenerator) -> None:
    out = await _call(hass, "cast")
    assert out["rating_key"] == "12345"
    assert [p["name"] for p in out["people"]] == ["Ellen Pompeo", "Sandra Oh", "Chandra Wilson"]
    assert out["people"][0]["role"] == "Meredith Grey"
    assert "picture" not in out["people"][2]  # no headshot
    picture = out["people"][1]["picture"]
    assert picture.startswith(f"/api/media_player_proxy/{PLAYER}/browse_media/cue_headshot/Sandra")
    # The picture comes through HA's proxy, Cue's token added by HA.
    client = await hass_client()
    resp = await client.get(picture)
    assert resp.status == HTTPStatus.OK and await resp.read() == JPEG + b"Sandra Oh"
    assert ("GET", "/v1/cast/Sandra Oh/headshot", None) in fake_cue.requests


async def test_on_screen(hass: HomeAssistant, fake_cue: FakeCue, playing, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cue_media_player.cue_const, "ON_SCREEN_POLL", 0)
    fake_cue.player["state"] = "paused"
    fake_cue.analyzing_polls = 2
    out = await _call(hass, "on_screen")
    assert [p["name"] for p in out["people"]] == ["Sandra Oh"]
    assert out["people"][0]["role"] == "Cristina Yang" and out["analyzing"] is False
    assert fake_cue.count("/v1/onstage/now") == 3


async def test_on_screen_refusals(hass: HomeAssistant, fake_cue: FakeCue, playing) -> None:
    with pytest.raises(HomeAssistantError, match="Pause first"):
        await _call(hass, "on_screen")
    fake_cue.on_screen_refusal = (409, "consent_required", "\"Share Who's on Screen with Home Assistant\" is off.")
    with pytest.raises(HomeAssistantError, match="won't say who's on screen"):
        await _call(hass, "on_screen")


async def test_filmography(hass: HomeAssistant, fake_cue: FakeCue, playing) -> None:
    out = await _call(hass, "filmography", person="Sandra Oh")
    assert out["name"] == "Sandra Oh"
    assert [i["media_content_id"] for i in out["items"]] == [f"{SERVER_ID}:12345", f"{SERVER_ID}:100"]
    assert ("GET", "/v1/people/Sandra Oh/credits", None) in fake_cue.requests


async def test_older_build(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    fake_cue.before_slice_g()
    fake_cue.play()
    await setup_entry(hass, make_entry(fake_cue))
    for service, data in (("cast", {}), ("on_screen", {}), ("filmography", {"person": "x"})):
        with pytest.raises(ServiceValidationError, match="newer Cue"):
            await _call(hass, service, **data)
