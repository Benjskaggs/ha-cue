"""Polls Cue's `/v1/status` and carries player commands."""

from __future__ import annotations

import asyncio
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
    CueRateLimitedError,
    CueResponseError,
)
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

type CueConfigEntry = ConfigEntry[CueCoordinator]


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
        if self.auth_failed:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="auth_failed"
            )
        self._raise_if_rate_limited(HomeAssistantError)

        async with self._command_lock:
            try:
                status = await self.client.command(name, body)
            except CueConnectionError as err:
                self._generation += 1
                self.async_set_updated_data(CueData(reachable=False))
                raise HomeAssistantError(
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
                    translation_key="command_failed",
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
