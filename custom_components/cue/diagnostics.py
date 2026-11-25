"""Diagnostics for Cue. The token never leaves."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_TOKEN
from homeassistant.core import HomeAssistant

from .coordinator import CueConfigEntry

TO_REDACT = {CONF_TOKEN, "Authorization", "authorization"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: CueConfigEntry
) -> dict[str, Any]:
    """Entry, options and the last status (which carries no secrets)."""
    coordinator = entry.runtime_data
    data = coordinator.data
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
            "unique_id": entry.unique_id,
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "last_exception": repr(coordinator.last_exception)
            if coordinator.last_exception
            else None,
            "auth_failed": coordinator.auth_failed,
            "update_interval_s": coordinator.update_interval.total_seconds()
            if coordinator.update_interval
            else None,
        },
        "info": {
            "id": coordinator.info.device_id,
            "version": coordinator.info.version,
            "features": sorted(coordinator.info.features),
            "lists_features": coordinator.info.lists_features,
        }
        if coordinator.info
        else None,
        "reachable": data.reachable if data else None,
        "status": async_redact_data(data.status, TO_REDACT) if data and data.status else None,
    }
