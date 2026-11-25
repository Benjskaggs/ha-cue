"""Polls Cue's `/v1/status` and carries player commands."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    CueAuthError,
    CueClient,
    CueConnectionError,
    CueError,
    CueRateLimitedError,
    CueResponseError,
)
from .const import ART_CACHE_MAX_BYTES, ART_CACHE_SIZE, DOMAIN, ENTITY_FEATURES, LEGACY_FEATURES

_LOGGER = logging.getLogger(__name__)

type CueConfigEntry = ConfigEntry[CueCoordinator]


# Cue error codes with their own message (the rest use `command_failed`).
ERROR_CODE_KEYS = {
    "nothing_playing": "nothing_playing",
    "no_marker": "no_marker",
    "unknown_track": "unknown_track",
    "no_next": "no_next",
    "no_previous": "no_previous",
}


class _NotOpen(HomeAssistantError):
    """Cue didn't answer: it isn't open."""


def _now() -> float:
    """Wall-clock seconds (HA's clock, so tests can move it)."""
    return dt_util.utcnow().timestamp()


@dataclass(slots=True)
class CueData:
    """One snapshot of the Apple TV.

    `reachable` False means Cue isn't open (tvOS suspends it in the
    background, and the listener dies with it). That's a normal state, shown
    as `off`, not an error.
    """

    reachable: bool
    status: dict[str, Any] | None = None

    @property
    def player(self) -> dict[str, Any] | None:
        """The `player` object, or None when idle or Cue isn't open."""
        if not self.reachable or not self.status:
            return None
        player = self.status.get("player")
        return player if isinstance(player, dict) else None


@dataclass(frozen=True, slots=True)
class CueInfo:
    """`GET /v1/info`, as far as the integration cares."""

    device_id: str | None
    name: str | None
    version: str | None
    features: frozenset[str]
    # False for builds before Slice B, which send neither `features` nor `id`.
    lists_features: bool

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> CueInfo:
        raw = data.get("features")
        listed = isinstance(raw, list)
        device_id = data.get("id")
        app = data.get("app") if isinstance(data.get("app"), dict) else {}
        return cls(
            device_id=device_id if isinstance(device_id, str) and device_id else None,
            name=data.get("name") if isinstance(data.get("name"), str) else None,
            version=app.get("version"),
            features=frozenset(str(f) for f in raw) if listed else LEGACY_FEATURES,
            lists_features=listed,
        )


class CueCoordinator(DataUpdateCoordinator[CueData]):
    """Owns the client, the poll, and the per-entry command queue."""

    config_entry: CueConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: CueConfigEntry,
        client: CueClient,
        scan_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {client.host}",
            update_interval=timedelta(seconds=scan_interval),
        )
        self.client = client
        self._command_lock = asyncio.Lock()
        # Set on the first 401. From then on nothing is sent to the Apple TV
        # until reauth reloads the entry (scout §5: the limiter is per IP).
        self.auth_failed = False
        self._blocked_until = 0.0
        # Bumped whenever a command reply is applied, so a poll that was in
        # flight across a command can't overwrite the fresher reply.
        self._generation = 0
        # `/v1/info`, once Cue has answered it (None until then: Cue wasn't
        # open at setup). Entities are built from `features`.
        self.info: CueInfo | None = None
        self._late_info_started = False
        self.options_at_setup = dict(entry.options)
        # Couch-card art by (kind, ratingKey), fetched on first request for
        # an item and kept (LRU): never per poll.
        self._art_cache: OrderedDict[tuple[str, str], tuple[bytes, str]] = OrderedDict()
        self._art_inflight: dict[tuple[str, str], asyncio.Future[tuple[bytes, str] | None]] = {}

    def has(self, feature: str) -> bool:
        """Whether this build serves the v1 route `feature` (`stop`, …)."""
        features = self.info.features if self.info else LEGACY_FEATURES
        return feature in features

    async def async_fetch_info(self) -> CueInfo | None:
        """Ask Cue who it is. None when it isn't open (or won't say)."""
        try:
            self.info = CueInfo.from_json(await self.client.get_info())
        except CueError as err:
            _LOGGER.debug("No /v1/info from Cue: %s", err)
            return None
        return self.info

    def needs_reload_for(self, info: CueInfo) -> bool:
        """Entities were built without `info`: reload if it changes them.

        That's a stable id to adopt (the entry was keyed on the host) or a
        different set of the features that decide which entities exist.
        """
        entry = self.config_entry
        if info.device_id and entry.unique_id != info.device_id:
            other = self.hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, info.device_id)
            if other is None:
                return True
        return (info.features & ENTITY_FEATURES) != (LEGACY_FEATURES & ENTITY_FEATURES)

    async def _async_late_info(self) -> None:
        """Cue wasn't open at setup; now it is. Learn it, reload if needed."""
        # Imported here: issues imports this module's types.
        from .issues import async_sync_outdated_issue

        info = await self.async_fetch_info()
        if info is None:
            self._late_info_started = False
            return
        async_sync_outdated_issue(self.hass, self.config_entry, info)
        if self.needs_reload_for(info):
            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)

    async def _async_update_data(self) -> CueData:
        if self.auth_failed:
            raise ConfigEntryAuthFailed("Cue refused the token")
        self._raise_if_rate_limited(UpdateFailed)

        generation = self._generation
        try:
            status = await self.client.get_status()
        except CueConnectionError as err:
            _LOGGER.debug("Cue isn't open: %s", err)
            return CueData(reachable=False)
        except CueAuthError as err:
            self.auth_failed = True
            raise ConfigEntryAuthFailed("Cue refused the token") from err
        except CueRateLimitedError as err:
            self._block(err.retry_after)
            raise UpdateFailed(
                f"Cue is refusing this address for {err.retry_after} s (too many wrong tokens)"
            ) from err
        except CueResponseError as err:
            # 403 not_lan, 503 disabled/signed_out, …: Cue is open but won't
            # talk to us. Unavailable, logged once by the coordinator.
            raise UpdateFailed(f"Cue answered {err.status} {err.code}: {err.message}") from err

        if self.info is None and not self._late_info_started:
            self._late_info_started = True
            self.config_entry.async_create_background_task(
                self.hass, self._async_late_info(), f"{DOMAIN} info"
            )
        if generation != self._generation and self.data is not None:
            return self.data
        return CueData(reachable=True, status=status)

    async def async_command(
        self, name: str, body: dict[str, Any] | None = None
    ) -> None:
        """Send one player command and apply the status it answers with.

        Commands for one entry run one at a time (Cue serves one request per
        connection, 16 at most). The client gives each 5 s.
        """
        await self._async_guarded(name, lambda: self.client.command(name, body))

    async def async_read_now(self) -> None:
        """A fresh `/v1/status`, applied at once (for `cue.get_status`).

        Cue not open is not an error here: the data just goes unreachable.
        """
        try:
            await self._async_guarded("status", self.client.get_status)
        except _NotOpen:
            return

    async def _async_guarded(
        self, name: str, request: Callable[[], Awaitable[dict[str, Any]]]
    ) -> None:
        if self.auth_failed:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="auth_failed"
            )
        self._raise_if_rate_limited(HomeAssistantError)

        async with self._command_lock:
            try:
                status = await request()
            except CueConnectionError as err:
                self._generation += 1
                self.async_set_updated_data(CueData(reachable=False))
                raise _NotOpen(
                    translation_domain=DOMAIN, translation_key="not_open"
                ) from err
            except CueAuthError as err:
                self.auth_failed = True
                self.config_entry.async_start_reauth(self.hass)
                # Stop the poll too; reauth reloads the entry.
                self.async_update_listeners()
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="auth_failed"
                ) from err
            except CueRateLimitedError as err:
                self._block(err.retry_after)
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="rate_limited",
                    translation_placeholders={"retry_after": str(err.retry_after)},
                ) from err
            except CueResponseError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key=ERROR_CODE_KEYS.get(err.code or "", "command_failed"),
                    translation_placeholders={
                        "command": name,
                        "code": err.code or str(err.status),
                        "message": err.message,
                    },
                ) from err

            self._generation += 1
            self.async_set_updated_data(CueData(reachable=True, status=status))

    async def async_get_artwork(self, path: str) -> tuple[bytes, str] | None:
        """Artwork bytes through Cue's proxy, or None (never raises)."""
        if self.auth_failed or _now() < self._blocked_until:
            return None
        try:
            return await self.client.get_artwork(path)
        except CueAuthError:
            self.auth_failed = True
            self.config_entry.async_start_reauth(self.hass)
            return None
        except CueRateLimitedError as err:
            self._block(err.retry_after)
            return None
        except (CueConnectionError, CueResponseError) as err:
            _LOGGER.debug("No artwork from Cue: %s", err)
            return None

    def _playing_key(self) -> str | None:
        player = self.data.player if self.data else None
        item = (player or {}).get("item") or {}
        key = item.get("ratingKey")
        return str(key) if key is not None else None

    async def async_get_item_artwork(self, kind: str, rating_key: str) -> tuple[bytes, str] | None:
        """`kind` art of item `rating_key`, cached per (kind, ratingKey).

        Cue's `/v1/artwork/{kind}` always answers for the item playing *now*
        (the key is only a cache key), so bytes are only fetched, and only
        cached, while that item is the one playing: an image for another item
        is never stored under this key. Bytes pass through untouched (a
        clearLogo keeps its PNG alpha).
        """
        cache_key = (kind, rating_key)
        if cached := self._art_cache.get(cache_key):
            self._art_cache.move_to_end(cache_key)
            return cached
        if inflight := self._art_inflight.get(cache_key):
            return await inflight
        if self._playing_key() != rating_key:
            return None
        player = self.data.player if self.data else None
        path = ((player or {}).get("artwork") or {}).get(kind)
        if not isinstance(path, str):
            return None

        future: asyncio.Future[tuple[bytes, str] | None] = self.hass.loop.create_future()
        self._art_inflight[cache_key] = future
        try:
            result = await self.async_get_artwork(path)
            if result is not None and self._playing_key() != rating_key:
                result = None  # the item changed while we fetched
            if result is not None and len(result[0]) <= ART_CACHE_MAX_BYTES:
                self._art_cache[cache_key] = result
                while len(self._art_cache) > ART_CACHE_SIZE:
                    self._art_cache.popitem(last=False)
            future.set_result(result)
            return result
        except BaseException:
            # `async_get_artwork` never raises; only a cancellation lands
            # here. Anyone sharing this fetch is cancelled with it.
            future.cancel()
            raise
        finally:
            self._art_inflight.pop(cache_key, None)

    def _block(self, seconds: int) -> None:
        self._blocked_until = _now() + seconds

    def _raise_if_rate_limited(self, exc: type[Exception]) -> None:
        remaining = self._blocked_until - _now()
        if remaining <= 0:
            return
        if exc is HomeAssistantError:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="rate_limited",
                translation_placeholders={"retry_after": str(int(remaining) + 1)},
            )
        raise exc(f"Waiting {int(remaining) + 1} s for Cue's rate limit to clear")
