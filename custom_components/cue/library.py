"""Search results and Continue Watching as Home Assistant media (Cue 1.2.1).

Cue's `/v1/search` and `/v1/hubs/continue` answer `MediaItem`s (openapi):
`ratingKey`, `type`, `title`, `server`, and for episodes `show`, `season`,
`episode`. Their `media_content_id` is `<server>:<ratingKey>`, the same
form the media player reports and `play_media` takes.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.media_player import BrowseMedia, MediaClass, MediaType

from .const import BROWSE_CONTINUE, BROWSE_ROOT

# Cue item type → (media class, media content type).
TYPES: dict[str, tuple[MediaClass, MediaType]] = {
    "movie": (MediaClass.MOVIE, MediaType.MOVIE),
    "show": (MediaClass.TV_SHOW, MediaType.TVSHOW),
    "season": (MediaClass.SEASON, MediaType.SEASON),
    "episode": (MediaClass.EPISODE, MediaType.EPISODE),
}

# A media_filter_classes entry (Assist's `media_class` slot) → Cue's
# search `type`. Anything else (music, podcasts, …) Cue doesn't have.
CLASS_TO_TYPE: dict[str, str] = {
    MediaClass.MOVIE: "movie",
    MediaClass.TV_SHOW: "show",
    MediaClass.EPISODE: "episode",
}

# Content types where "play this" means "carry on with the show".
RESUME_TYPES = {MediaType.TVSHOW, MediaType.SEASON, "show", "season", "tvshow"}


def media_id(item: dict[str, Any]) -> str:
    """`<server>:<ratingKey>`, or the bare key without a server."""
    key = str(item.get("ratingKey"))
    server = item.get("server")
    return f"{server}:{key}" if server else key


def item_title(item: dict[str, Any]) -> str:
    """`The Matrix (1999)`, `Grey's Anatomy · S5 E15 · Before and After`."""
    title = str(item.get("title") or item.get("ratingKey"))
    kind = item.get("type")
    show = (item.get("show") or {}).get("title")
    if kind == "episode" and show:
        number = " ".join(
            part
            for part in (
                f"S{item['season']}" if item.get("season") is not None else None,
                f"E{item['episode']}" if item.get("episode") is not None else None,
            )
            if part
        )
        return " · ".join(p for p in (show, number, title) if p)
    if kind == "season" and show:
        return f"{show} · {title}"
    if item.get("year"):
        return f"{title} ({item['year']})"
    return title


def browse_item(item: dict[str, Any]) -> BrowseMedia:
    """A playable leaf. No thumbnail: Cue only proxies the playing item's art."""
    media_class, content_type = TYPES.get(str(item.get("type")), (MediaClass.VIDEO, MediaType.VIDEO))
    return BrowseMedia(
        media_class=media_class,
        media_content_id=media_id(item),
        media_content_type=content_type,
        title=item_title(item),
        can_play=True,
        can_expand=False,
    )


def continue_folder(children: list[BrowseMedia] | None = None) -> BrowseMedia:
    return BrowseMedia(
        media_class=MediaClass.DIRECTORY,
        media_content_id=BROWSE_CONTINUE,
        media_content_type=MediaType.VIDEO,
        title="Continue Watching",
        can_play=False,
        can_expand=True,
        children=children,
        children_media_class=MediaClass.VIDEO,
    )


def root(title: str) -> BrowseMedia:
    return BrowseMedia(
        media_class=MediaClass.DIRECTORY,
        media_content_id=BROWSE_ROOT,
        media_content_type=MediaType.VIDEO,
        title=title,
        can_play=False,
        can_expand=True,
        children=[continue_folder()],
        children_media_class=MediaClass.DIRECTORY,
    )


def with_ids(items: list[Any]) -> list[dict[str, Any]]:
    """Cue's items for a service response, each with the `media_content_id`
    and `media_content_type` to hand to `media_player.play_media`."""
    out = []
    for item in items:
        if not isinstance(item, dict) or item.get("ratingKey") is None:
            continue
        _, content_type = TYPES.get(str(item.get("type")), (MediaClass.VIDEO, MediaType.VIDEO))
        out.append({
            **item,
            "media_content_id": media_id(item),
            "media_content_type": str(content_type),
            "display_title": item_title(item),
        })
    return out
