"""Cue's player as a Home Assistant media player."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
import hashlib
import logging
import re
import time
from typing import Any
from urllib.parse import quote

from homeassistant.components.media_player import (
    ATTR_APP_NAME,
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
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import APPLE_TV_APP_NAME, CONF_APPLE_TV_ENTITY, DOMAIN
from .coordinator import CueConfigEntry, CueCoordinator
from .entity import CueEntity

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

_RATING_KEY = re.compile(r"^\d+$")
_SERVER_ID = re.compile(r"^[A-Za-z0-9_-]+$")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CueConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the media player."""
    async_add_entities([CueMediaPlayer(entry.runtime_data)])


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


def cue_play_url(rating_key: str, server: str | None) -> str:
    """The deep link Cue's DeepLinkRouter accepts."""
    url = f"cue://play/{rating_key}"
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
        if self._apple_tv_entity:
            features |= MediaPlayerEntityFeature.PLAY_MEDIA
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
        return attrs

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
        """Close the player (builds that list `stop`)."""
        await self.coordinator.async_command("stop")

    # MARK: play_media, cold start through HA's apple_tv integration

    @property
    def _apple_tv_entity(self) -> str | None:
        return self.coordinator.config_entry.options.get(CONF_APPLE_TV_ENTITY)

    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        """Play a Plex ratingKey in Cue, starting Cue if it isn't open.

        Order matters: bring Cue to the front through the linked `apple_tv`
        entity first, then send `cue://play/…`. A link sent to an app that
        isn't frontmost makes tvOS ask "Open in Cue?". Success is only
        reported once Cue's own status shows the item playing.
        """
        atv = self._apple_tv_entity
        if not atv:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="no_apple_tv"
            )
        rating_key, server = parse_media_id(media_id, kwargs.get("extra"))
        url = cue_play_url(rating_key, server)

        async with self._play_media_lock:
            await self._async_cold_start(atv, url, rating_key)

    async def _async_cold_start(self, atv: str, url: str, rating_key: str) -> None:
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
            if self._is_playing(rating_key):
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

    def _is_playing(self, rating_key: str) -> bool:
        player = self.player
        if not player:
            return False
        item = player.get("item") or {}
        return (
            str(item.get("ratingKey")) == rating_key
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
