"""Repair issues."""

from __future__ import annotations

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, ISSUE_OUTDATED
from .coordinator import CueConfigEntry, CueInfo


def _issue_id(entry: CueConfigEntry) -> str:
    return f"{ISSUE_OUTDATED}_{entry.entry_id}"


@callback
def async_sync_outdated_issue(hass: HomeAssistant, entry: CueConfigEntry, info: CueInfo) -> None:
    """Raise "Cue is out of date" for a build without `/v1/info.features`
    (before Slice B: no stable id, and the per-IP lockout that a stale token
    elsewhere on this host can trip). Clear it once a newer build answers."""
    if info.lists_features:
        ir.async_delete_issue(hass, DOMAIN, _issue_id(entry))
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(entry),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_OUTDATED,
        translation_placeholders={
            "name": entry.title,
            "version": info.version or "?",
        },
    )


@callback
def async_delete_issues(hass: HomeAssistant, entry: CueConfigEntry) -> None:
    """The entry is gone."""
    ir.async_delete_issue(hass, DOMAIN, _issue_id(entry))
