"""Cue's player as a Home Assistant media player."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
import hashlib
import logging
import re
import time
from typing import Any
from urllib.parse import quote

import voluptuous as vol

from homeassistant.components.media_player import (
    ATTR_APP_NAME,
    BrowseError,
    BrowseMedia,
    SearchError,
    SearchMedia,
    SearchMediaQuery,
    ATTR_INPUT_SOURCE,
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    DOMAIN as MEDIA_PLAYER_DOMAIN,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    SERVICE_PLAY_MEDIA,
    SERVICE_SELECT_SOURCE,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_STANDBY,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    ServiceResponse,
    State,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_platform, service as ha_service
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import (
    APPLE_TV_APP_NAME,
    ART_CONTENT_PREFIX,
    ART_KINDS,
    ATTR_ACCENT_COLOR,
    BROWSE_CONTINUE,
    BROWSE_ROOT,
    CONF_APPLE_TV_ENTITY,
    DOMAIN,
    FEATURE_COLORS,
    FEATURE_CAST,
    FEATURE_CONTINUE,
    FEATURE_CREDITS,
    FEATURE_HEADSHOT,
    FEATURE_ON_SCREEN,
    FEATURE_OPEN,
    FEATURE_SEARCH,
    HEADSHOT_CONTENT_TYPE,
)
from . import const as cue_const
from . import library
from .coordinator import CueConfigEntry, CueCoordinator
from .entity import CueEntity
from .legacy import NOT_OPEN, error_status, find_track, v0_status

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# Cold start timings. Module-level so tests can shrink them.
WAKE_TIMEOUT = 10.0
APP_TIMEOUT = 10.0
# tvOS needs a moment after the app is frontmost before a link lands in it
# rather than prompting (Ben's `cue_open` script waits 1 s too).
SETTLE_DELAY = 1.0
# Cue keeps an autoplay request alive for 30 s; give it most of that.
VERIFY_TIMEOUT = 25.0
VERIFY_POLL = 1.0

STATE_MAP = {
    "playing": MediaPlayerState.PLAYING,
    "paused": MediaPlayerState.PAUSED,
    "buffering": MediaPlayerState.BUFFERING,
    "loading": MediaPlayerState.BUFFERING,
    "ended": MediaPlayerState.IDLE,
    "failed": MediaPlayerState.IDLE,
}
# Player states that mean "the item we asked for is on screen and going".
STARTED_STATES = {"loading", "playing", "paused", "buffering"}

TYPE_MAP = {
    "episode": MediaType.EPISODE,
    "movie": MediaType.MOVIE,
    "clip": MediaType.VIDEO,
}

SLEEPING_STATES = {STATE_OFF, STATE_STANDBY, STATE_UNAVAILABLE, STATE_UNKNOWN}
# HA feature ← the Cue route (`/v1/info.features`) it needs.
ROUTE_FEATURES = (
    ("play", MediaPlayerEntityFeature.PLAY),
    ("pause", MediaPlayerEntityFeature.PAUSE),
    ("seek", MediaPlayerEntityFeature.SEEK),
    ("stop", MediaPlayerEntityFeature.STOP),
)

# Entity services (`cue.<name>`). Each answers with Cue's status in the v0
# shape (`legacy.v0_status`), so `script.cue_control` keeps its response.
SERVICE_GET_STATUS = "get_status"
SERVICE_SELECT_SUBTITLE = "select_subtitle"
SERVICE_SELECT_AUDIO = "select_audio"
SERVICE_SET_RATE = "set_rate"
SERVICE_SKIP = "skip"
SERVICE_SEARCH = "search"
SERVICE_CONTINUE_WATCHING = "continue_watching"
SERVICE_CAST = "cast"
SERVICE_ON_SCREEN = "on_screen"
SERVICE_FILMOGRAPHY = "filmography"
ATTR_PERSON = "person"
ATTR_QUERY = "query"
ATTR_TYPE = "type"
ATTR_LIMIT = "limit"
ATTR_TRACK_ID = "track_id"
ATTR_LANGUAGE = "language"
ATTR_OFF = "off"
ATTR_RATE = "rate"
ATTR_MARKER = "marker"
# True: a Cue error comes back in the response (`ok: false`, `error`) instead
# of being raised. For scripts that must never stop on an error.
ATTR_RETURN_ERRORS = "return_errors"

_RETURN_ERRORS = {vol.Optional(ATTR_RETURN_ERRORS, default=False): cv.boolean}

_RATING_KEY = re.compile(r"^\d+$")
_ACCENT = re.compile(r"^#[0-9A-Fa-f]{6}$")
_SERVER_ID = re.compile(r"^[A-Za-z0-9_-]+$")


SERVICES: tuple[tuple[str, dict[Any, Any], str, SupportsResponse], ...] = (
    (SERVICE_GET_STATUS, {}, "async_service_get_status", SupportsResponse.ONLY),
    (
        SERVICE_SELECT_SUBTITLE,
        {
            vol.Optional(ATTR_TRACK_ID): cv.string,
            vol.Optional(ATTR_LANGUAGE): cv.string,
            vol.Optional(ATTR_OFF): cv.boolean,
            **_RETURN_ERRORS,
        },
        "async_service_select_subtitle",
        SupportsResponse.OPTIONAL,
    ),
    (
        SERVICE_SELECT_AUDIO,
        {
            vol.Optional(ATTR_TRACK_ID): cv.string,
            vol.Optional(ATTR_LANGUAGE): cv.string,
            **_RETURN_ERRORS,
        },
        "async_service_select_audio",
        SupportsResponse.OPTIONAL,
    ),
    (
        SERVICE_SET_RATE,
        {
            vol.Required(ATTR_RATE): vol.All(
                vol.Coerce(float), vol.Range(min=0, min_included=False, max=4)
            ),
            **_RETURN_ERRORS,
        },
        "async_service_set_rate",
        SupportsResponse.OPTIONAL,
    ),
    (
        SERVICE_SKIP,
        {vol.Required(ATTR_MARKER): vol.In(["intro", "credits"]), **_RETURN_ERRORS},
        "async_service_skip",
        SupportsResponse.OPTIONAL,
    ),
    (
        SERVICE_SEARCH,
        {
            vol.Required(ATTR_QUERY): vol.All(cv.string, vol.Length(min=1, max=200)),
            vol.Optional(ATTR_TYPE): vol.All(cv.ensure_list, [vol.In(["movie", "show", "episode"])]),
            vol.Optional(ATTR_LIMIT): vol.All(vol.Coerce(int), vol.Range(min=1, max=50)),
        },
        "async_service_search",
        SupportsResponse.ONLY,
    ),
    (SERVICE_CONTINUE_WATCHING, {}, "async_service_continue_watching", SupportsResponse.ONLY),
    (SERVICE_CAST, {}, "async_service_cast", SupportsResponse.ONLY),
    (SERVICE_ON_SCREEN, {}, "async_service_on_screen", SupportsResponse.ONLY),
    (
        SERVICE_FILMOGRAPHY,
        {vol.Required(ATTR_PERSON): vol.All(cv.string, vol.Length(min=1, max=200))},
        "async_service_filmography",
        SupportsResponse.ONLY,
    ),
)

HAS_PLATFORM_SERVICE_HELPER = hasattr(ha_service, "async_register_platform_entity_service")


@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Register the cue.* services at integration setup, so they exist even
    while no entry is loaded (a 401 at startup): a call then fails with a
    HomeAssistantError, which a script's continue_on_error catches, instead
    of ServiceNotFound, which it can't."""
    if not HAS_PLATFORM_SERVICE_HELPER:
        return
    for name, schema, method, response in SERVICES:
        ha_service.async_register_platform_entity_service(
            hass,
            DOMAIN,
            name,
            entity_domain=MEDIA_PLAYER_DOMAIN,
            schema=schema,
            func=method,
            supports_response=response,
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CueConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the media player and the `cue.*` entity services."""
    async_add_entities([CueMediaPlayer(entry.runtime_data)])

    # The cue.* services are registered once, in `async_setup`
    # (`async_register_services`); on HA builds without the integration-level
    # helper they're registered here instead.
    if not HAS_PLATFORM_SERVICE_HELPER:
        platform = entity_platform.async_get_current_platform()
        for name, schema, method, response in SERVICES:
            platform.async_register_entity_service(name, schema, method, supports_response=response)


def parse_media_id(media_id: str, extra: dict[str, Any] | None) -> tuple[str, str | None]:
    """`12345`, or `<server machineId>:12345` (what this entity reports).

    `extra.server` also names the server. Raises ServiceValidationError.
    """
    media_id = str(media_id).strip()
    server = (extra or {}).get("server")
    if ":" in media_id:
        server, _, media_id = media_id.rpartition(":")
    if not _RATING_KEY.match(media_id):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="bad_media_id",
            translation_placeholders={"media_id": media_id},
        )
    if server is not None:
        server = str(server).strip()
        if not _SERVER_ID.match(server):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="bad_media_id",
                translation_placeholders={"media_id": f"{server}:{media_id}"},
            )
    return media_id, server or None


def cue_play_url(rating_key: str, server: str | None, *, resume: bool = False) -> str:
    """The deep link Cue's DeepLinkRouter accepts (`cue://resume/…` carries
    on with a show)."""
    url = f"cue://{'resume' if resume else 'play'}/{rating_key}"
    if server:
        url += f"?server={quote(server, safe='')}"
    return url


class CueMediaPlayer(CueEntity, MediaPlayerEntity):
    """Cue's player on one Apple TV."""

    _attr_name = None
    _attr_media_image_remotely_accessible = False

    def __init__(self, coordinator: CueCoordinator) -> None:
        super().__init__(coordinator, None)
        self._play_media_lock = asyncio.Lock()

    # MARK: state

    @property
    def state(self) -> MediaPlayerState:
        """`off` when Cue isn't open, `idle` when nothing is playing."""
        data = self.coordinator.data
        if data is None or not data.reachable:
            return MediaPlayerState.OFF
        player = data.player
        if player is None:
            return MediaPlayerState.IDLE
        return STATE_MAP.get(str(player.get("state")), MediaPlayerState.IDLE)

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        """What this Cue build serves (`features`), next/previous only when
        Cue has one queued, play_media only with a linked apple_tv."""
        has = self.coordinator.has
        features = MediaPlayerEntityFeature(0)
        for route, feature in ROUTE_FEATURES:
            if has(route):
                features |= feature
        if self._apple_tv_entity or has(FEATURE_OPEN):
            features |= MediaPlayerEntityFeature.PLAY_MEDIA
        if has(FEATURE_SEARCH):
            features |= MediaPlayerEntityFeature.SEARCH_MEDIA
        if has(FEATURE_CONTINUE):
            features |= MediaPlayerEntityFeature.BROWSE_MEDIA
        queue = (self.player or {}).get("queue") or {}
        if queue.get("next") and has("next"):
            features |= MediaPlayerEntityFeature.NEXT_TRACK
        if queue.get("previous") and has("previous"):
            features |= MediaPlayerEntityFeature.PREVIOUS_TRACK
        return features

    @property
    def app_name(self) -> str | None:
        """Always Cue, while it's open."""
        return APPLE_TV_APP_NAME if self.player is not None else None

    @property
    def _item(self) -> dict[str, Any]:
        player = self.player
        item = player.get("item") if player else None
        return item if isinstance(item, dict) else {}

    @property
    def media_content_id(self) -> str | None:
        """`<server machineId>:<ratingKey>`: keys only mean something per server."""
        item = self._item
        rating_key = item.get("ratingKey")
        if not rating_key:
            return None
        server = item.get("server")
        return f"{server}:{rating_key}" if server else str(rating_key)

    @property
    def media_content_type(self) -> MediaType | str | None:
        """episode / movie / video."""
        kind = self._item.get("type")
        if not kind:
            return None
        return TYPE_MAP.get(kind, kind)

    @property
    def media_title(self) -> str | None:
        """The item's own title (the episode title for an episode)."""
        return self._item.get("title")

    @property
    def media_series_title(self) -> str | None:
        """The show, for an episode."""
        show = self._item.get("show")
        return show.get("title") if isinstance(show, dict) else None

    @property
    def media_season(self) -> str | None:
        """Season number."""
        season = self._item.get("season")
        return str(season) if season is not None else None

    @property
    def media_episode(self) -> str | None:
        """Episode number."""
        episode = self._item.get("episode")
        return str(episode) if episode is not None else None

    @property
    def media_duration(self) -> float | None:
        """Seconds."""
        player = self.player
        if not player or player.get("durationMs") is None:
            return None
        return player["durationMs"] / 1000

    @property
    def media_position(self) -> float | None:
        """Seconds, as of `media_position_updated_at`."""
        player = self.player
        if not player or player.get("positionMs") is None:
            return None
        return player["positionMs"] / 1000

    @property
    def media_position_updated_at(self) -> datetime | None:
        """Cue's own `positionAt`, so HA extrapolates from the TV's clock."""
        player = self.player
        if not player:
            return None
        raw = player.get("positionAt")
        parsed = dt_util.parse_datetime(raw) if isinstance(raw, str) else None
        return parsed or dt_util.utcnow()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Speed, year, server and what's up next."""
        player = self.player
        if not player:
            return None
        item = self._item
        attrs: dict[str, Any] = {"rate": player.get("rate")}
        if item.get("year") is not None:
            attrs["year"] = item["year"]
        if item.get("server"):
            attrs["server"] = item["server"]
        nxt = (player.get("queue") or {}).get("next")
        if isinstance(nxt, dict):
            attrs["next_title"] = nxt.get("title")
            attrs["next_rating_key"] = nxt.get("ratingKey")
        attrs.update(self._art_attributes(player))
        return attrs

    def _art_attributes(self, player: dict[str, Any]) -> dict[str, Any]:
        """The couch-card attributes (cue-art-contract §2), each only when
        this build lists the feature AND the item has that art."""
        has = self.coordinator.has
        out: dict[str, Any] = {}
        accent = (player.get("colors") or {}).get("accent")
        if has(FEATURE_COLORS) and isinstance(accent, str) and _ACCENT.match(accent):
            out[ATTR_ACCENT_COLOR] = accent.upper()
        rating_key = self._item.get("ratingKey")
        artwork = player.get("artwork") or {}
        if not rating_key or not self.hass:
            return out
        for kind, attribute, feature in ART_KINDS:
            if not isinstance(artwork.get(kind), str):
                continue
            if feature is not None and not has(feature):
                continue
            out[attribute] = self.get_browse_image_url(f"{ART_CONTENT_PREFIX}{kind}", str(rating_key))
        return out

    async def async_get_browse_image(
        self,
        media_content_type: str,
        media_content_id: str,
        media_image_id: str | None = None,
    ) -> tuple[bytes | None, str | None]:
        """HA's image proxy for the `entity_picture_*` URLs: Cue's
        `/v1/artwork/<kind>`, with the token, from the per-item cache."""
        if media_content_type == HEADSHOT_CONTENT_TYPE:
            if not self.coordinator.has(FEATURE_HEADSHOT):
                return None, None
            path = f"/v1/cast/{quote(media_content_id, safe='')}/headshot"
            result = await self.coordinator.async_get_artwork(path)
            return result if result is not None else (None, None)
        if not media_content_type.startswith(ART_CONTENT_PREFIX):
            return None, None
        kind = media_content_type[len(ART_CONTENT_PREFIX):]
        feature = next((f for k, _, f in ART_KINDS if k == kind), "unknown")
        if feature == "unknown" or (feature is not None and not self.coordinator.has(feature)):
            return None, None
        result = await self.coordinator.async_get_item_artwork(kind, media_content_id)
        return result if result is not None else (None, None)

    # MARK: artwork, proxied through HA (the image needs the token)

    def _artwork_path(self) -> str | None:
        player = self.player
        artwork = (player or {}).get("artwork") or {}
        for kind in ("still", "art", "poster"):
            if path := artwork.get(kind):
                return path
        return None

    @property
    def media_image_hash(self) -> str | None:
        """Changes with the item, so HA's proxy re-fetches."""
        if not self.coordinator.has("artwork"):
            return None
        path = self._artwork_path()
        if path is None:
            return None
        return hashlib.sha256(path.encode()).hexdigest()[:16]

    async def async_get_media_image(self) -> tuple[bytes | None, str | None]:
        """Fetch `/v1/artwork/<kind>` with the token."""
        path = self._artwork_path()
        if path is None:
            return None, None
        result = await self.coordinator.async_get_artwork(path)
        if result is None:
            return None, None
        return result

    # MARK: commands

    async def async_media_play(self) -> None:
        """Resume."""
        await self.coordinator.async_command("play")

    async def async_media_pause(self) -> None:
        """Pause."""
        await self.coordinator.async_command("pause")

    async def async_media_seek(self, position: float) -> None:
        """Seek to `position` seconds."""
        await self.coordinator.async_command(
            "seek", {"positionMs": max(0, round(position * 1000))}
        )

    async def async_media_next_track(self) -> None:
        """Next episode."""
        await self.coordinator.async_command("next")

    async def async_media_previous_track(self) -> None:
        """Previous episode (builds that list `previous`)."""
        await self.coordinator.async_command("previous")

    async def async_media_stop(self) -> None:
        """Close the player (builds that list `stop`). Nothing playing is
        what stop asks for: a stop that lands mid-hop between episodes (409
        `nothing_playing`) is a success, not an error."""
        try:
            await self.coordinator.async_command("stop")
        except HomeAssistantError as err:
            if getattr(err, "translation_key", None) != "nothing_playing":
                raise
            await self.coordinator.async_read_now()

    # MARK: cue.* entity services (v0-shaped status responses)

    def _v0(self) -> dict[str, Any]:
        data = self.coordinator.data
        if data is None or not data.reachable:
            return error_status(NOT_OPEN)
        return v0_status(data.status, picture=self.entity_picture)

    async def async_service_get_status(self) -> ServiceResponse:
        """`cue.get_status`: a fresh read, never an exception (an error
        comes back as `{ok: false, active: false, error}`)."""
        try:
            await self.coordinator.async_read_now()
        except HomeAssistantError as err:
            return error_status(str(err))
        return self._v0()

    async def _async_run(self, return_errors: bool, action: Callable[[], Awaitable[None]]) -> ServiceResponse:
        try:
            await action()
        except HomeAssistantError as err:
            if not return_errors:
                raise
            return {**self._v0(), "ok": False, "error": str(err)}
        return self._v0()

    async def _async_tracks(self, kind: str) -> list[dict[str, Any]]:
        """The current `kind` tracks, from a fresh read."""
        await self.coordinator.async_read_now()
        player = self.player
        if player is None:
            data = self.coordinator.data
            key = "nothing_playing" if data and data.reachable else "not_open"
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key=key)
        block = player.get(kind) or {}
        return [t for t in block.get("tracks") or [] if isinstance(t, dict)]

    async def _async_pick(self, kind: str, track_id: str | None, language: str | None) -> str:
        if track_id:
            return track_id
        assert language is not None
        tracks = await self._async_tracks(kind)
        track = find_track(tracks, language)
        if track is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_track_for_language",
                translation_placeholders={
                    "kind": "subtitle" if kind == "subtitles" else "audio",
                    "language": language,
                    "tracks": ", ".join(str(t.get("title")) for t in tracks) or "none",
                },
            )
        return str(track["id"])

    async def async_service_select_subtitle(
        self,
        track_id: str | None = None,
        language: str | None = None,
        off: bool = False,
        return_errors: bool = False,
    ) -> ServiceResponse:
        """`cue.select_subtitle`: a track id, a language, or off."""
        _require_one(track_id=track_id, language=language, off=off or None)

        async def action() -> None:
            if off:
                await self.coordinator.async_command("subtitles", {"id": None})
                return
            chosen = await self._async_pick("subtitles", track_id, language)
            await self.coordinator.async_command("subtitles", {"id": chosen})

        return await self._async_run(return_errors, action)

    async def async_service_select_audio(
        self,
        track_id: str | None = None,
        language: str | None = None,
        return_errors: bool = False,
    ) -> ServiceResponse:
        """`cue.select_audio`: a track id or a language."""
        _require_one(track_id=track_id, language=language)

        async def action() -> None:
            chosen = await self._async_pick("audio", track_id, language)
            await self.coordinator.async_command("audio", {"id": chosen})

        return await self._async_run(return_errors, action)

    async def async_service_set_rate(self, rate: float, return_errors: bool = False) -> ServiceResponse:
        """`cue.set_rate`: speed for this item, (0, 4]."""
        return await self._async_run(
            return_errors, lambda: self.coordinator.async_command("rate", {"rate": rate})
        )

    async def async_service_skip(self, marker: str, return_errors: bool = False) -> ServiceResponse:
        """`cue.skip`: past the intro or credits."""
        return await self._async_run(
            return_errors, lambda: self.coordinator.async_command("skip", {"marker": marker})
        )

    # MARK: play_media, cold start through HA's apple_tv integration

    @property
    def _apple_tv_entity(self) -> str | None:
        return self.coordinator.config_entry.options.get(CONF_APPLE_TV_ENTITY)

    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        """Play a Plex ratingKey in Cue.

        With Cue open on a 1.2.1+ build: `/v1/open`, then wait for
        `status.lastOpen` to say a player came up. A show or season resumes
        where you are in it.

        With Cue not open: start it through the linked `apple_tv` entity.
        Order matters: bring Cue to the front first, then send
        `cue://play/…`; a link sent to an app that isn't frontmost makes tvOS
        ask "Open in Cue?". Success is only reported once Cue's own status
        shows the item playing.
        """
        extra = kwargs.get("extra") or {}
        rating_key, server = parse_media_id(media_id, extra)
        resume = bool(extra.get("resume", str(media_type) in library.RESUME_TYPES))
        atv = self._apple_tv_entity
        async with self._play_media_lock:
            data = self.coordinator.data
            if data is not None and data.reachable and self.coordinator.has(FEATURE_OPEN):
                await self._async_open(rating_key, server, resume)
                return
            if not atv:
                if data is not None and data.reachable:
                    raise ServiceValidationError(
                        translation_domain=DOMAIN, translation_key="no_apple_tv"
                    )
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="not_open_no_apple_tv"
                )
            url = cue_play_url(rating_key, server, resume=resume)
            await self._async_cold_start(atv, url, rating_key, resume=resume)

    async def _async_open(self, rating_key: str, server: str | None, resume: bool) -> None:
        """`/v1/open`, then wait for `status.lastOpen` to resolve."""
        body: dict[str, Any] = {"ratingKey": rating_key}
        if server:
            body["server"] = server
        if resume:
            body["resume"] = True
        client = self.coordinator.client
        accepted = await self.coordinator.async_library("open", lambda: client.open(body))
        request_id = accepted.get("requestId")
        deadline = time.monotonic() + VERIFY_TIMEOUT
        while True:
            await self.coordinator.async_read_now()
            status = (self.coordinator.data.status or {}) if self.coordinator.data else {}
            last = status.get("lastOpen") or {}
            if last.get("requestId") == request_id and last.get("result") not in (None, "pending"):
                if last["result"] == "started":
                    return
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="open_failed",
                    translation_placeholders={
                        "rating_key": rating_key,
                        "result": str(last["result"]),
                        "message": str(last.get("message") or ""),
                    },
                )
            if time.monotonic() >= deadline or self.coordinator.auth_failed:
                break
            await asyncio.sleep(VERIFY_POLL)
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="play_not_started",
            translation_placeholders={"url": f"/v1/open {rating_key}", "seconds": str(int(VERIFY_TIMEOUT))},
        )

    # MARK: search and browse (Cue 1.2.1)

    async def async_search_media(self, query: SearchMediaQuery) -> SearchMedia:
        """Assist's "play X on the TV" (`HassMediaSearchAndPlay`) lands here,
        then plays the first result."""
        types = [library.CLASS_TO_TYPE[c] for c in query.media_filter_classes or [] if c in library.CLASS_TO_TYPE]
        if query.media_filter_classes and not types:
            return SearchMedia(result=[])
        try:
            items = await self._async_search(query.search_query, types, 10)
        except HomeAssistantError as err:
            raise SearchError(str(err)) from err
        return SearchMedia(result=[library.browse_item(i) for i in items])

    async def _async_search(self, text: str, types: list[str] | None, limit: int | None) -> list[dict[str, Any]]:
        self._require(FEATURE_SEARCH)
        client = self.coordinator.client
        reply = await self.coordinator.async_library("search", lambda: client.search(text, types, limit))
        return [i for i in reply.get("results") or [] if isinstance(i, dict)]

    async def _async_continue(self) -> list[dict[str, Any]]:
        self._require(FEATURE_CONTINUE)
        client = self.coordinator.client
        reply = await self.coordinator.async_library("continue", client.continue_watching)
        return [i for i in reply.get("items") or [] if isinstance(i, dict)]

    def _require(self, feature: str) -> None:
        if not self.coordinator.has(feature):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="needs_newer_cue",
                translation_placeholders={"feature": feature},
            )

    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Cue › Continue Watching."""
        if media_content_id in (None, "", BROWSE_ROOT):
            return library.root(self.coordinator.config_entry.title or "Cue")
        if media_content_id == BROWSE_CONTINUE:
            try:
                items = await self._async_continue()
            except HomeAssistantError as err:
                raise BrowseError(str(err)) from err
            return library.continue_folder([library.browse_item(i) for i in items])
        raise BrowseError(f"Unknown Cue folder {media_content_id}")

    async def async_service_search(
        self, query: str, type: list[str] | None = None, limit: int | None = None
    ) -> ServiceResponse:
        """`cue.search`: titles on Cue's Plex server; never plays anything.
        Each result carries the `media_content_id` for `play_media`."""
        items = await self._async_search(query, type, limit)
        return {"query": query, "results": library.with_ids(items)}

    def _people(self, people: Any) -> list[dict[str, Any]]:
        """Cue's people, the headshot path swapped for HA's proxy URL."""
        out = []
        for person in people or []:
            if not isinstance(person, dict) or not person.get("name"):
                continue
            entry = {k: person[k] for k in ("name", "role", "actorKey") if person.get(k) is not None}
            if person.get("headshot") and person.get("actorKey"):
                entry["picture"] = self.get_browse_image_url(HEADSHOT_CONTENT_TYPE, str(person["actorKey"]))
            out.append(entry)
        return out

    async def async_service_cast(self) -> ServiceResponse:
        """`cue.cast`: the playing item's billed cast (Plex metadata, no
        face recognition)."""
        self._require(FEATURE_CAST)
        client = self.coordinator.client
        reply = await self.coordinator.async_library("cast", client.get_cast)
        return {"rating_key": reply.get("ratingKey"), "people": self._people(reply.get("people"))}

    async def async_service_on_screen(self) -> ServiceResponse:
        """`cue.on_screen`: who Onstage recognised in the paused frame. Only
        with Cue's "Share Who's on Screen" switch on, Onstage on, and
        playback paused; waits a few seconds while Onstage is still looking."""
        self._require(FEATURE_ON_SCREEN)
        client = self.coordinator.client
        deadline = time.monotonic() + cue_const.ON_SCREEN_WAIT
        while True:
            reply = await self.coordinator.async_library("on_screen", client.get_on_screen)
            if not reply.get("analyzing") or time.monotonic() >= deadline:
                break
            await asyncio.sleep(cue_const.ON_SCREEN_POLL)
        return {
            "rating_key": reply.get("ratingKey"),
            "people": self._people(reply.get("people")),
            "analyzing": bool(reply.get("analyzing")),
        }

    async def async_service_filmography(self, person: str) -> ServiceResponse:
        """`cue.filmography`: what else `person` (a name, or an actorKey from
        cue.cast / cue.on_screen) is in, in Cue's Plex library."""
        self._require(FEATURE_CREDITS)
        client = self.coordinator.client
        reply = await self.coordinator.async_library("filmography", lambda: client.get_credits(person))
        return {"name": reply.get("name") or person, "items": library.with_ids(reply.get("items") or [])}

    async def async_service_continue_watching(self) -> ServiceResponse:
        """`cue.continue_watching`: Cue Home's Continue Watching row."""
        return {"items": library.with_ids(await self._async_continue())}

    async def _async_cold_start(self, atv: str, url: str, rating_key: str, *, resume: bool = False) -> None:
        hass = self.hass
        state = hass.states.get(atv)
        if state is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="apple_tv_missing",
                translation_placeholders={"entity_id": atv},
            )

        # 1. Wake the Apple TV if it's asleep.
        if state.state in SLEEPING_STATES:
            await hass.services.async_call(
                MEDIA_PLAYER_DOMAIN,
                SERVICE_TURN_ON,
                {ATTR_ENTITY_ID: atv},
                blocking=True,
            )
            if not await _async_wait_for_state(
                hass, atv, lambda s: s.state not in SLEEPING_STATES, WAKE_TIMEOUT
            ):
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="wake_failed",
                    translation_placeholders={"entity_id": atv},
                )

        # 2. Bring Cue to the front, and wait until tvOS says it is.
        state = hass.states.get(atv)
        if state is None or state.attributes.get(ATTR_APP_NAME) != APPLE_TV_APP_NAME:
            await hass.services.async_call(
                MEDIA_PLAYER_DOMAIN,
                SERVICE_SELECT_SOURCE,
                {ATTR_ENTITY_ID: atv, ATTR_INPUT_SOURCE: APPLE_TV_APP_NAME},
                blocking=True,
            )
            if not await _async_wait_for_state(
                hass,
                atv,
                lambda s: s.attributes.get(ATTR_APP_NAME) == APPLE_TV_APP_NAME,
                APP_TIMEOUT,
            ):
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="launch_failed",
                    translation_placeholders={"entity_id": atv},
                )
            if SETTLE_DELAY:
                await asyncio.sleep(SETTLE_DELAY)

        # 3. The deep link, now that it lands in a frontmost Cue.
        await hass.services.async_call(
            MEDIA_PLAYER_DOMAIN,
            SERVICE_PLAY_MEDIA,
            {
                ATTR_ENTITY_ID: atv,
                ATTR_MEDIA_CONTENT_TYPE: MediaType.URL,
                ATTR_MEDIA_CONTENT_ID: url,
            },
            blocking=True,
        )

        # 4. Never claim success that didn't happen: wait until Cue itself
        # reports this item. A `server=` that isn't Cue's connected server
        # only shows the page, and that's a failure too.
        deadline = time.monotonic() + VERIFY_TIMEOUT
        while True:
            if self._is_playing(None if resume else rating_key):
                return
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(VERIFY_POLL)
            await self.coordinator.async_refresh()
            if self.coordinator.auth_failed:
                break
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="play_not_started",
            translation_placeholders={"url": url, "seconds": str(int(VERIFY_TIMEOUT))},
        )

    def _is_playing(self, rating_key: str | None) -> bool:
        """`rating_key` is playing (None: anything is, for a resumed show,
        which starts one of its episodes)."""
        player = self.player
        if not player:
            return False
        item = player.get("item") or {}
        return (
            (rating_key is None or str(item.get("ratingKey")) == rating_key)
            and player.get("state") in STARTED_STATES
        )


async def _async_wait_for_state(
    hass: HomeAssistant,
    entity_id: str,
    predicate: Callable[[State], bool],
    timeout: float,
) -> bool:
    """True once `entity_id`'s state satisfies `predicate`, False on timeout."""
    current = hass.states.get(entity_id)
    if current is not None and predicate(current):
        return True

    done = asyncio.Event()

    @callback
    def _changed(event: Event[EventStateChangedData]) -> None:
        new = event.data["new_state"]
        if new is not None and predicate(new):
            done.set()

    unsub = async_track_state_change_event(hass, [entity_id], _changed)
    try:
        # The state may have changed between the first check and subscribing.
        current = hass.states.get(entity_id)
        if current is not None and predicate(current):
            return True
        async with asyncio.timeout(timeout):
            await done.wait()
        return True
    except TimeoutError:
        return False
    finally:
        unsub()


def _require_one(**choices: Any) -> None:
    """Exactly one of the choices must be given."""
    given = [name for name, value in choices.items() if value not in (None, "", False)]
    if len(given) != 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="no_choice",
            translation_placeholders={"fields": ", ".join(choices)},
        )
