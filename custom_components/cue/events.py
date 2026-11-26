"""Playback events: what changed between two snapshots of the Apple TV.

The same rules run on every update, pushed (`/v1/events`, Cue 1.2.1) or
polled, so automations see one set of events either way. Pushed, they land
within half a second (Cue samples twice a second and sends a status the
moment a marker is entered); polled, up to a poll late.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .coordinator import CueData

MARKER_EVENTS = {"intro": "intro_started", "credits": "credits_started"}


def _app(data: CueData | None) -> dict[str, Any]:
    app = (data.status or {}).get("app") if data and data.reachable else None
    return app if isinstance(app, dict) else {}


def _on_screen(data: CueData | None) -> bool:
    return bool(data and data.reachable) and _app(data).get("foreground") is not False


def describe(player: dict[str, Any] | None) -> dict[str, Any]:
    """The item an event is about, for the event's data."""
    if not player:
        return {}
    item = player.get("item") or {}
    out: dict[str, Any] = {
        "rating_key": item.get("ratingKey"),
        "media_type": item.get("type"),
        "title": item.get("title"),
        "state": player.get("state"),
    }
    show = (item.get("show") or {}).get("title")
    if show:
        out.update(series=show, season=item.get("season"), episode=item.get("episode"))
    return {k: v for k, v in out.items() if v is not None}


def transitions(old: CueData | None, new: CueData) -> list[tuple[str, dict[str, Any]]]:
    """`(event type, data)` for each change from `old` to `new`."""
    out: list[tuple[str, dict[str, Any]]] = []
    if old is None:
        return out
    if _on_screen(old) and not _on_screen(new):
        out.append(("foreground_lost", {}))
    # No baseline (Cue wasn't answering) or no news (it stopped): nothing
    # else can be said.
    if not (old.reachable and new.reachable):
        return out

    before, after = old.player, new.player
    if before is None and after is not None:
        out.append(("started", describe(after)))
        return out
    if before is not None and after is None:
        out.append(("stopped", describe(before)))
        return out
    if before is None or after is None:
        return out

    key_before = (before.get("item") or {}).get("ratingKey")
    key_after = (after.get("item") or {}).get("ratingKey")
    if key_before != key_after:
        out.append(("item_changed", describe(after)))
        return out

    state_before, state_after = before.get("state"), after.get("state")
    if state_after == "ended" and state_before != "ended":
        out.append(("ended", describe(after)))
    elif state_before == "playing" and state_after == "paused":
        out.append(("paused", describe(after)))
    elif state_before == "paused" and state_after == "playing":
        out.append(("resumed", describe(after)))

    was_active = {
        m.get("type"): bool(m.get("active"))
        for m in before.get("markers") or []
        if isinstance(m, dict)
    }
    for marker in after.get("markers") or []:
        if not isinstance(marker, dict):
            continue
        kind = marker.get("type")
        if kind in MARKER_EVENTS and marker.get("active") and not was_active.get(kind, False):
            out.append((MARKER_EVENTS[kind], describe(after)))
    return out
