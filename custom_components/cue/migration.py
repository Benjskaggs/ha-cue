"""Moving an entry from its provisional host key to Cue's stable id."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import DOMAIN
from .coordinator import CueConfigEntry

_LOGGER = logging.getLogger(__name__)


@callback
def async_adopt_device_id(hass: HomeAssistant, entry: CueConfigEntry, device_id: str) -> None:
    """Re-key an entry created before Cue had an id (keyed on the host)."""
    if entry.unique_id == device_id:
        return
    other = hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, device_id)
    if other is not None and other.entry_id != entry.entry_id:
        _LOGGER.warning(
            "Cue at %s reports id %s, which entry %s already has; leaving %s as it is",
            entry.data.get("host"),
            device_id,
            other.title,
            entry.title,
        )
        return
    hass.config_entries.async_update_entry(entry, unique_id=device_id)


@callback
def async_migrate_registries(hass: HomeAssistant, entry: CueConfigEntry) -> None:
    """Point this entry's entities and device at its current unique_id.

    Entity unique ids are `<base>` (the media player) and `<base>_<key>`, and
    the device is `(cue, <base>)`. When the base changes (host → stable id)
    the registry entries are renamed in place, so entity ids, names, areas
    and history stay put. Runs before the platforms set up.
    """
    new_base = entry.unique_id or entry.entry_id
    ent_reg = er.async_get(hass)
    registered = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    player = next((e for e in registered if e.domain == "media_player"), None)
    if player is None or player.unique_id == new_base:
        return
    old_base = player.unique_id
    for reg_entry in registered:
        if reg_entry.unique_id == old_base:
            new_unique_id = new_base
        elif reg_entry.unique_id.startswith(f"{old_base}_"):
            new_unique_id = new_base + reg_entry.unique_id[len(old_base):]
        else:
            continue
        if ent_reg.async_get_entity_id(reg_entry.domain, DOMAIN, new_unique_id):
            _LOGGER.warning("Not migrating %s: %s is taken", reg_entry.entity_id, new_unique_id)
            continue
        ent_reg.async_update_entity(reg_entry.entity_id, new_unique_id=new_unique_id)

    dev_reg = dr.async_get(hass)
    device = next(
        (d for d in dr.async_entries_for_config_entry(dev_reg, entry.entry_id) if (DOMAIN, old_base) in d.identifiers),
        None,
    )
    if device:
        dev_reg.async_update_device(device.id, new_identifiers={(DOMAIN, new_base)})
    _LOGGER.info("Cue: moved %s from %s to its stable id %s", entry.title, old_base, new_base)
