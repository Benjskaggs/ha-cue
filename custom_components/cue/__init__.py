"""The Cue integration: Cue's Control API on an Apple TV."""

from __future__ import annotations

from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL, CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import CueClient
from .const import DEFAULT_PORT, DEFAULT_SCAN_INTERVAL
from .coordinator import CueConfigEntry, CueCoordinator

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.MEDIA_PLAYER,
    Platform.SELECT,
]


async def async_setup_entry(hass: HomeAssistant, entry: CueConfigEntry) -> bool:
    """Set up one Apple TV."""
    client = CueClient(
        async_get_clientsession(hass),
        entry.data[CONF_HOST],
        entry.data.get(CONF_PORT, DEFAULT_PORT),
        entry.data[CONF_TOKEN],
    )
    coordinator = CueCoordinator(
        hass,
        entry,
        client,
        entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
    )
    # Unreachable is fine here (Cue isn't open: state `off`). A 401 raises
    # ConfigEntryAuthFailed, which starts reauth instead of retrying.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CueConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_options_updated(hass: HomeAssistant, entry: CueConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
