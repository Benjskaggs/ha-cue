"""The Cue integration: Cue's Control API on an Apple TV."""

from __future__ import annotations

from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL, CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import CueClient
from .const import DEFAULT_PORT, DEFAULT_SCAN_INTERVAL, DOMAIN
from .coordinator import CueConfigEntry, CueCoordinator
from .issues import async_delete_issues, async_sync_outdated_issue
from .media_player import async_register_services
from .migration import async_adopt_device_id, async_migrate_registries

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.MEDIA_PLAYER,
    Platform.SELECT,
]


CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the cue.* services (they act on Cue media players)."""
    async_register_services(hass)
    return True


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
    # Who it is and what it can do. Unreachable is fine (Cue isn't open);
    # the coordinator asks again on its first good poll.
    if info := await coordinator.async_fetch_info():
        if info.device_id:
            async_adopt_device_id(hass, entry, info.device_id)
        async_sync_outdated_issue(hass, entry, info)
    # Entities and the device follow the entry's unique_id (host, or the
    # stable id once Cue has one). Before the platforms set up.
    async_migrate_registries(hass, entry)

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


async def async_remove_entry(hass: HomeAssistant, entry: CueConfigEntry) -> None:
    """Drop this entry's repair issue."""
    async_delete_issues(hass, entry)


async def _async_options_updated(hass: HomeAssistant, entry: CueConfigEntry) -> None:
    # Only for options: flows that change `data` (reauth, a new host from
    # zeroconf) reload the entry themselves.
    if dict(entry.options) != entry.runtime_data.options_at_setup:
        await hass.config_entries.async_reload(entry.entry_id)
