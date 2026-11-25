"""Cue's frozen v0 `/status` shape, rebuilt from v1 status.

`cue.get_status` (and every Cue service that answers with status) returns
this, so `script.cue_control`, the Jarvis prompt and the stage media card keep
reading the keys they always have, while only this integration talks to the
TV. The shape is `ControlStatus.snapshot` in Cue's
`Cue/Features/Control/ControlServer.swift`: same keys and types. A few keys
are added at the end (`picture`, `source`); v0 readers ignore them.
"""

from __future__ import annotations

from typing import Any

NOT_OPEN = "Cue isn't open on the Apple TV, or Home Assistant Control is off."


def error_status(message: str) -> dict[str, Any]:
    """What `script.cue_control` always answered when Cue didn't."""
    return {"ok": False, "active": False, "error": message}


def v0_status(status: dict[str, Any] | None, *, picture: str | None = None) -> dict[str, Any]:
    """v1 `/v1/status` → the v0 dictionary. `status` None = Cue unreachable."""
    if status is None:
        return error_status(NOT_OPEN)
    player = status.get("player")
    if not isinstance(player, dict):
        return {"ok": True, "active": False, "state": "idle", "source": "cue_integration"}

    item = player.get("item") or {}
    queue = player.get("queue") or {}
    state = str(player.get("state") or "idle")

    out: dict[str, Any] = {
        "ok": True,
        "active": True,
        "state": state,
        "playing": state == "playing",
        "ratingKey": item.get("ratingKey"),
        "type": item.get("type"),
        "title": item.get("title"),
        "positionMs": player.get("positionMs"),
        "durationMs": player.get("durationMs"),
        "rate": player.get("rate"),
        "server": item.get("server"),
        "subtitles": _tracks(player.get("subtitles"), subtitles=True),
        "audio": _tracks(player.get("audio"), subtitles=False),
        "markers": {
            "intro": _marker(player.get("markers"), "intro"),
            "credits": _marker(player.get("markers"), "credits"),
        },
        "hasNext": bool(queue.get("next")),
    }
    if item.get("type") == "episode":
        show = item.get("show") or {}
        out["show"] = show.get("title")
        out["season"] = item.get("season")
        out["episode"] = item.get("episode")
        out["showRatingKey"] = show.get("ratingKey")
    if item.get("year") is not None:
        out["year"] = item["year"]
    # v0 listed `/artwork?kind=…` paths on the TV itself. Under the one-road
    # rule nothing calls the TV directly, so only the cache key stays, and the
    # picture is HA's own image proxy (`picture`, below).
    out["artwork"] = {"key": item.get("ratingKey")}
    nxt = queue.get("next")
    if isinstance(nxt, dict):
        n: dict[str, Any] = {"ratingKey": nxt.get("ratingKey"), "title": nxt.get("title")}
        if nxt.get("season") is not None:
            n["season"] = nxt["season"]
        if nxt.get("episode") is not None:
            n["episode"] = nxt["episode"]
        out["next"] = n
    if picture:
        out["picture"] = picture
    out["source"] = "cue_integration"
    return out


def _tracks(block: Any, *, subtitles: bool) -> dict[str, Any]:
    block = block if isinstance(block, dict) else {}
    selected = block.get("selected")
    tracks = []
    for track in block.get("tracks") or []:
        if not isinstance(track, dict):
            continue
        t: dict[str, Any] = {
            "id": track.get("id"),
            "title": track.get("title"),
            "selected": track.get("id") == selected if selected is not None else bool(track.get("selected")),
        }
        if track.get("language") is not None:
            t["language"] = track["language"]
        if subtitles and track.get("system"):
            t["system"] = True
        tracks.append(t)
    return {"selected": selected, "tracks": tracks}


def _marker(markers: Any, kind: str) -> dict[str, Any] | None:
    for marker in markers or []:
        if isinstance(marker, dict) and marker.get("type") == kind:
            return {
                "startMs": marker.get("startMs"),
                "endMs": marker.get("endMs"),
                "active": bool(marker.get("active")),
                "skippable": bool(marker.get("skippable")),
            }
    return None


# ISO 639-2 codes whose 639-1 form isn't their first two letters.
_ISO3_TO_2 = {
    "spa": "es", "fre": "fr", "fra": "fr", "ger": "de", "deu": "de", "jpn": "ja",
    "chi": "zh", "zho": "zh", "kor": "ko", "dut": "nl", "nld": "nl", "swe": "sv",
    "dan": "da", "gre": "el", "ell": "el", "heb": "he", "per": "fa", "fas": "fa",
    "cze": "cs", "ces": "cs", "pol": "pl", "tur": "tr", "ara": "ar", "ukr": "uk",
}


def _code(value: str) -> str:
    """`en`, `eng`, `en-US` → `en`; anything longer is returned as is."""
    value = value.strip().lower().replace("_", "-").split("-")[0]
    if len(value) == 3:
        return _ISO3_TO_2.get(value, value[:2])
    return value


def find_track(tracks: list[dict[str, Any]], language: str) -> dict[str, Any] | None:
    """The first track in `language`: a code (`en`, `eng`, `en-US`) or a
    name (`English`, matched against the language field or the title). Real
    tracks before system-made ones (auto-transcription, CC)."""
    wanted = language.strip().lower()
    if not wanted:
        return None
    ordered = sorted(tracks, key=lambda t: bool(t.get("system")))

    def lang(t: dict[str, Any]) -> str:
        return str(t.get("language") or "").strip().lower()

    def title(t: dict[str, Any]) -> str:
        return str(t.get("title") or "").lower()

    is_code = len(_code(wanted)) == 2 and len(wanted.split("-")[0]) <= 3
    matchers = [lambda t: lang(t) == wanted]
    if is_code:
        matchers.append(lambda t: len(_code(lang(t))) == 2 and _code(lang(t)) == _code(wanted))
    else:
        matchers.append(lambda t: title(t).startswith(wanted))
        matchers.append(lambda t: wanted in title(t))
    for match in matchers:
        for track in ordered:
            if match(track):
                return track
    return None
