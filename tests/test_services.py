"""`cue.get_status` and the command services, in Cue's v0 response shape, and
`script.cue_control` (from the jarvis repo) on top of them."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.setup import async_setup_component

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cue.legacy import find_track, v0_status

from .conftest import PLAYER, make_entry, setup_entry
from .fake_cue import FakeCue, playing_status

SCRIPT_FILE = Path.home() / "Developer/home/jarvis/ha/cue_control_setup.py"
LIVE_ENTITY = "media_player.cue_living_room"

# Every key `ControlStatus.snapshot` (Cue, ControlServer.swift) sends for an episode.
V0_KEYS = {
    "ok", "active", "state", "playing", "ratingKey", "type", "title", "positionMs", "durationMs",
    "rate", "server", "subtitles", "audio", "markers", "hasNext", "show", "season", "episode",
    "showRatingKey", "year", "artwork", "next",
}


@pytest.fixture
async def playing(hass: HomeAssistant, fake_cue: FakeCue) -> MockConfigEntry:
    fake_cue.play()
    entry = make_entry(fake_cue)
    await setup_entry(hass, entry)
    return entry


async def _call(hass: HomeAssistant, service: str, **data: Any) -> dict[str, Any]:
    response = await hass.services.async_call(
        "cue", service, {"entity_id": PLAYER, **data}, blocking=True, return_response=True
    )
    assert set(response) == {PLAYER}
    return response[PLAYER]


# MARK: the v0 shape


def test_v0_shape_matches_cue_snapshot() -> None:
    """Same keys and types as Cue's v0 /status for a playing episode."""
    out = v0_status(playing_status(), picture="/api/media_player_proxy/x?token=t")
    assert set(out) >= V0_KEYS
    assert set(out) - V0_KEYS == {"picture", "source"}
    assert out["ok"] is True and out["active"] is True and out["playing"] is True
    assert out["state"] == "playing"
    assert (out["ratingKey"], out["type"], out["title"]) == ("12345", "episode", "Before and After")
    assert (out["show"], out["season"], out["episode"], out["showRatingKey"]) == ("Grey's Anatomy", 5, 15, "100")
    assert isinstance(out["positionMs"], int) and isinstance(out["durationMs"], int)
    assert out["rate"] == 1.0
    assert out["subtitles"] == {
        "selected": "3",
        "tracks": [
            {"id": "3", "title": "English (SRT)", "selected": True, "language": "en"},
            {"id": "4", "title": "Spanish (SRT)", "selected": False, "language": "es"},
        ],
    }
    assert out["audio"]["selected"] == "1" and "system" not in out["audio"]["tracks"][0]
    assert out["markers"] == {
        "intro": {"startMs": 60000, "endMs": 95000, "active": False, "skippable": False},
        "credits": {"startMs": 2500000, "endMs": 2583000, "active": False, "skippable": False},
    }
    assert out["hasNext"] is True
    assert out["next"] == {"ratingKey": "12346", "title": "Rise Up", "season": 5, "episode": 16}
    assert out["artwork"] == {"key": "12345"}


def test_v0_shape_idle_and_closed() -> None:
    """Idle and unreachable answer as v0 / the old script did."""
    idle = v0_status({"api": 1, "app": {}, "player": None})
    assert {k: idle[k] for k in ("ok", "active", "state")} == {"ok": True, "active": False, "state": "idle"}
    closed = v0_status(None)
    assert closed["ok"] is False and closed["active"] is False and closed["error"]


def test_v0_movie_has_no_episode_keys() -> None:
    status = playing_status()
    status["player"]["item"].update(type="movie", show=None, season=None, episode=None)
    out = v0_status(status)
    assert not {"show", "season", "episode", "showRatingKey"} & set(out)


def test_find_track() -> None:
    tracks = [
        {"id": "9", "title": "English (Auto)", "language": "en", "system": True},
        {"id": "3", "title": "English (SRT)", "language": "en"},
        {"id": "4", "title": "Spanish (SRT)", "language": "es"},
        {"id": "5", "title": "Deutsch", "language": "ger"},
    ]
    assert find_track(tracks, "en")["id"] == "3"  # a real track beats a system one
    assert find_track(tracks, "eng")["id"] == "3"
    assert find_track(tracks, "en-US")["id"] == "3"
    assert find_track(tracks, "spa")["id"] == "4"
    assert find_track(tracks, "Spanish")["id"] == "4"
    assert find_track(tracks, "de")["id"] == "5"
    assert find_track(tracks, "fr") is None
    assert find_track(tracks, "") is None


# MARK: cue.get_status


async def test_get_status_is_fresh(hass: HomeAssistant, fake_cue: FakeCue, playing) -> None:
    """A fresh read every call, keyed by entity id, v0-shaped."""
    before = fake_cue.count("/v1/status")
    fake_cue.player["positionMs"] = 1_200_000
    out = await _call(hass, "get_status")
    assert fake_cue.count("/v1/status") == before + 1
    assert out["positionMs"] == 1_200_000
    assert set(out) >= V0_KEYS
    assert out["picture"].startswith("/api/media_player_proxy/media_player.living_room?")
    # The read also refreshed the entity.
    assert hass.states.get(PLAYER).attributes["media_position"] == 1200.0


async def test_get_status_idle(hass: HomeAssistant, entry) -> None:
    out = await _call(hass, "get_status")
    assert (out["ok"], out["active"], out["state"]) == (True, False, "idle")


async def test_get_status_never_raises(hass: HomeAssistant, fake_cue: FakeCue, playing) -> None:
    """Closed, disabled, wrong token: an ok:false answer, not an exception."""
    await fake_cue.go_away()
    out = await _call(hass, "get_status")
    assert out["ok"] is False and out["active"] is False and "isn't open" in out["error"]
    assert hass.states.get(PLAYER).state == "off"

    await fake_cue.come_back()
    fake_cue.disabled = True
    out = await _call(hass, "get_status")
    assert out["ok"] is False and "disabled" in out["error"]

    fake_cue.disabled = False
    fake_cue.token = "REGENERATED"
    out = await _call(hass, "get_status")
    assert out["ok"] is False and "Re-authenticate" in out["error"]
    flows = hass.config_entries.flow.async_progress_by_handler("cue")
    assert [f["context"]["source"] for f in flows] == ["reauth"]
    # And after a 401 nothing more is sent.
    sent = len(fake_cue.requests)
    out = await _call(hass, "get_status")
    assert out["ok"] is False and len(fake_cue.requests) == sent


# MARK: commands


async def test_select_subtitle(hass: HomeAssistant, fake_cue: FakeCue, playing) -> None:
    out = await _call(hass, "select_subtitle", track_id="4")
    assert fake_cue.requests[-1] == ("POST", "/v1/player/subtitles", {"id": "4"})
    assert out["subtitles"]["selected"] == "4"

    out = await _call(hass, "select_subtitle", off=True)
    assert fake_cue.requests[-1] == ("POST", "/v1/player/subtitles", {"id": None})
    assert out["subtitles"]["selected"] is None

    out = await _call(hass, "select_subtitle", language="English")
    assert fake_cue.requests[-1] == ("POST", "/v1/player/subtitles", {"id": "3"})
    assert out["subtitles"]["selected"] == "3"

    with pytest.raises(ServiceValidationError, match="No subtitle track in fr"):
        await _call(hass, "select_subtitle", language="fr")
    with pytest.raises(ServiceValidationError, match="exactly one"):
        await _call(hass, "select_subtitle", track_id="3", off=True)
    with pytest.raises(ServiceValidationError, match="exactly one"):
        await _call(hass, "select_subtitle")


async def test_unknown_track(hass: HomeAssistant, fake_cue: FakeCue, playing) -> None:
    with pytest.raises(HomeAssistantError, match="no such track"):
        await _call(hass, "select_subtitle", track_id="99")
    out = await _call(hass, "select_audio", track_id="99", return_errors=True)
    assert out["ok"] is False and "no such track" in out["error"]
    assert out["active"] is True and out["audio"]["selected"] == "1"  # still the status


async def test_select_audio(hass: HomeAssistant, fake_cue: FakeCue, playing) -> None:
    out = await _call(hass, "select_audio", track_id="2")
    assert fake_cue.requests[-1] == ("POST", "/v1/player/audio", {"id": "2"})
    assert out["audio"]["selected"] == "2"
    out = await _call(hass, "select_audio", language="eng")
    assert out["audio"]["selected"] == "1"


async def test_set_rate(hass: HomeAssistant, fake_cue: FakeCue, playing) -> None:
    out = await _call(hass, "set_rate", rate=1.5)
    assert fake_cue.requests[-1] == ("POST", "/v1/player/rate", {"rate": 1.5})
    assert out["rate"] == 1.5
    with pytest.raises(vol.Invalid):
        await _call(hass, "set_rate", rate=5)
    with pytest.raises(vol.Invalid):
        await _call(hass, "set_rate", rate=0)


async def test_skip(hass: HomeAssistant, fake_cue: FakeCue, playing) -> None:
    fake_cue.player["positionMs"] = 10_000
    out = await _call(hass, "skip", marker="intro")
    assert fake_cue.requests[-1] == ("POST", "/v1/player/skip", {"marker": "intro"})
    assert out["positionMs"] == 95_000

    fake_cue.player["positionMs"] = 2_583_000
    with pytest.raises(HomeAssistantError, match="no intro or credits marker ahead"):
        await _call(hass, "skip", marker="credits")
    out = await _call(hass, "skip", marker="credits", return_errors=True)
    assert out["ok"] is False and "marker" in out["error"]


async def test_nothing_playing(hass: HomeAssistant, fake_cue: FakeCue, entry) -> None:
    with pytest.raises(HomeAssistantError, match="Nothing is playing"):
        await _call(hass, "set_rate", rate=1.25)
    with pytest.raises(HomeAssistantError, match="Nothing is playing"):
        await _call(hass, "select_subtitle", language="en")
    out = await _call(hass, "skip", marker="intro", return_errors=True)
    assert out["ok"] is False and out["active"] is False and "Nothing is playing" in out["error"]


async def test_command_when_closed(hass: HomeAssistant, fake_cue: FakeCue, playing) -> None:
    await fake_cue.go_away()
    out = await _call(hass, "set_rate", rate=1.25, return_errors=True)
    assert out["ok"] is False and out["active"] is False


# MARK: script.cue_control, as the jarvis repo defines it


def _script() -> dict[str, Any]:
    if not SCRIPT_FILE.exists():
        pytest.skip("jarvis repo not checked out here")
    tree = ast.parse(SCRIPT_FILE.read_text())
    node = next(
        n for n in tree.body
        if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "SCRIPT" for t in n.targets)
    )
    script = ast.literal_eval(node.value)
    # The live entity id → this test's.
    return json.loads(json.dumps(script).replace(LIVE_ENTITY, PLAYER))


@pytest.fixture
async def cue_control(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "script", {"script": {"cue_control": _script()}})
    await hass.async_block_till_done()


async def _script_call(hass: HomeAssistant, action: str, value: str | None = None) -> dict[str, Any]:
    data = {"action": action} if value is None else {"action": action, "value": value}
    return await hass.services.async_call(
        "script", "cue_control", data, blocking=True, return_response=True
    )


async def test_script_status(hass: HomeAssistant, fake_cue: FakeCue, playing, cue_control) -> None:
    out = await _script_call(hass, "status")
    assert set(out) >= V0_KEYS
    assert out["active"] is True and out["title"] == "Before and After"


async def test_script_transport(hass: HomeAssistant, fake_cue: FakeCue, playing, cue_control) -> None:
    out = await _script_call(hass, "pause")
    assert out["state"] == "paused" and out["playing"] is False
    out = await _script_call(hass, "play")
    assert out["playing"] is True
    out = await _script_call(hass, "seek", "600000")
    assert ("POST", "/v1/player/seek", {"positionMs": 600000}) in fake_cue.requests
    assert out["positionMs"] == 600000
    out = await _script_call(hass, "seek", "0")  # "restart this episode"
    assert out["positionMs"] == 0
    out = await _script_call(hass, "next")
    assert out["ratingKey"] == "12346" and out["hasNext"] is False


async def test_script_tracks_rate_skip(hass: HomeAssistant, fake_cue: FakeCue, playing, cue_control) -> None:
    out = await _script_call(hass, "subtitle", "4")
    assert out["subtitles"]["selected"] == "4"
    out = await _script_call(hass, "subtitle", "off")
    assert out["subtitles"]["selected"] is None
    out = await _script_call(hass, "audio", "2")
    assert out["audio"]["selected"] == "2"
    out = await _script_call(hass, "rate", "1.5")
    assert out["rate"] == 1.5
    fake_cue.player["positionMs"] = 10_000
    out = await _script_call(hass, "skip", "intro")
    assert out["positionMs"] == 95_000 and out["ok"] is True


async def test_script_errors_never_raise(hass: HomeAssistant, fake_cue: FakeCue, playing, cue_control) -> None:
    """Refusals and bad values come back as ok:false on top of the status."""
    fake_cue.player["positionMs"] = 2_583_000
    out = await _script_call(hass, "skip", "credits")
    assert out["ok"] is False and "marker" in out["error"] and out["active"] is True
    out = await _script_call(hass, "subtitle", "99")
    assert out["ok"] is False and "no such track" in out["error"]
    for action, value in (("seek", "abc"), ("rate", "9"), ("skip", "middle"), ("audio", ""), ("dance", None)):
        out = await _script_call(hass, action, value)
        assert out["ok"] is False and out["error"], (action, value)
    out = await _script_call(hass, "status")
    assert out["ok"] is True


async def test_script_when_cue_closed(hass: HomeAssistant, fake_cue: FakeCue, playing, cue_control) -> None:
    await fake_cue.go_away()
    for action, value in (("status", None), ("pause", None), ("rate", "1.25"), ("subtitle", "off")):
        out = await _script_call(hass, action, value)
        assert out["ok"] is False and out["active"] is False and out["error"], action


async def test_script_idle(hass: HomeAssistant, fake_cue: FakeCue, entry, cue_control) -> None:
    out = await _script_call(hass, "status")
    assert out["ok"] is True and out["active"] is False
    out = await _script_call(hass, "next")
    assert out["active"] is False


async def test_script_when_entry_failed_setup(
    hass: HomeAssistant, fake_cue: FakeCue, apple_tv, cue_control
) -> None:
    """A 401 at startup leaves no Cue entity: the services still exist, so the
    script answers ok:false instead of raising ServiceNotFound. (`apple_tv`
    loads the media_player domain, as Ben's apple_tv entity does.)"""
    entry = make_entry(fake_cue, token="WRONG")
    await setup_entry(hass, entry)
    assert hass.services.has_service("cue", "get_status")
    out = await _script_call(hass, "status")
    assert out["ok"] is False and out["active"] is False
    out = await _script_call(hass, "pause")
    assert out["ok"] is False
    assert fake_cue.failed_auth == 1  # the script sent nothing to the TV


async def test_services_on_older_ha(
    hass: HomeAssistant, fake_cue: FakeCue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without HA's integration-level helper the platform registers them."""
    from custom_components.cue import media_player as cue_mp

    monkeypatch.setattr(cue_mp, "HAS_PLATFORM_SERVICE_HELPER", False)
    fake_cue.play()
    await setup_entry(hass, make_entry(fake_cue))
    out = await _call(hass, "get_status")
    assert out["active"] is True
