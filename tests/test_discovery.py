"""Slice B: zeroconf, the stable id, migrating host-keyed entries, feature
detection and the "Cue is out of date" repair issue."""

from __future__ import annotations

from datetime import timedelta
from ipaddress import ip_address

from freezegun.api import FrozenDateTimeFactory

from homeassistant import config_entries
from homeassistant.components.media_player import (
    DOMAIN as MP_DOMAIN,
    SERVICE_MEDIA_STOP,
    MediaPlayerEntityFeature,
)
from homeassistant.const import ATTR_ENTITY_ID, CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.cue.const import DOMAIN

from .conftest import PLAYER, make_entry, setup_entry
from .fake_cue import DEVICE_ID, TOKEN, FakeCue

ISSUE = "outdated_cue"


def _zeroconf(
    host: str = "127.0.0.1",
    port: int = 7788,
    *,
    device_id: str | None = DEVICE_ID,
    api: str | None = "1",
    addresses: list[str] | None = None,
) -> ZeroconfServiceInfo:
    props = {"v": "1", "port": str(port), "name": "Living Room", "app": "1.2"}
    if api is not None:
        props["api"] = api
    if device_id:
        props["id"] = device_id
    ips = [ip_address(a) for a in (addresses or [host])]
    return ZeroconfServiceInfo(
        ip_address=ips[0],
        ip_addresses=ips,
        port=port,
        hostname="Living-Room.local.",
        type="_cue-control._tcp.local.",
        name="Living Room._cue-control._tcp.local.",
        properties=props,
    )


async def _discover(hass: HomeAssistant, info: ZeroconfServiceInfo):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=info
    )


def _entity_unique_ids(hass: HomeAssistant, entry_id: str) -> dict[str, str]:
    reg = er.async_get(hass)
    return {e.entity_id: e.unique_id for e in er.async_entries_for_config_entry(reg, entry_id)}


# MARK: zeroconf


async def test_zeroconf_new_device(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    """Discovered → token → an entry keyed on the TXT id."""
    result = await _discover(hass, _zeroconf(port=fake_cue.port))
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "token"
    assert result["description_placeholders"]["name"] == "Living Room"
    flow = hass.config_entries.flow.async_get(result["flow_id"])
    assert flow["context"]["unique_id"] == DEVICE_ID
    assert flow["context"]["title_placeholders"] == {"name": "Living Room"}
    assert fake_cue.requests == []  # TXT was enough; no request before the token

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: TOKEN})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == DEVICE_ID
    assert result["data"][CONF_HOST] == "127.0.0.1"
    assert result["data"][CONF_PORT] == fake_cue.port


async def test_zeroconf_updates_host(hass: HomeAssistant, socket_enabled: None) -> None:
    """The Apple TV came back on a new address: the entry follows it."""
    entry = make_entry(FakeCue(), unique_id=DEVICE_ID)
    entry.add_to_hass(hass)
    result = await _discover(hass, _zeroconf("192.168.1.77", 7788))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "192.168.1.77"
    assert entry.data[CONF_PORT] == 7788


async def test_zeroconf_adopts_host_keyed_entry(hass: HomeAssistant, socket_enabled: None) -> None:
    """An entry from before ids existed (keyed on its host) takes the id."""
    entry = make_entry(FakeCue())  # unique_id = 127.0.0.1
    entry.add_to_hass(hass)
    result = await _discover(hass, _zeroconf("127.0.0.1", 7788))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.unique_id == DEVICE_ID
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_zeroconf_old_build_keys_on_host(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    """A build without TXT `id`: the host stays the key."""
    fake_cue.legacy()
    result = await _discover(hass, _zeroconf(port=fake_cue.port, device_id=None, api=None))
    assert result["step_id"] == "token"
    assert hass.config_entries.flow.async_get(result["flow_id"])["context"]["unique_id"] == "127.0.0.1"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: TOKEN})
    assert result["result"].unique_id == "127.0.0.1"

    again = await _discover(hass, _zeroconf(port=fake_cue.port, device_id=None, api=None))
    assert again["type"] is FlowResultType.ABORT


async def test_zeroconf_refusals(hass: HomeAssistant) -> None:
    """A future API, or only a link-local IPv6 address, aborts."""
    result = await _discover(hass, _zeroconf(api="2"))
    assert result["reason"] == "unsupported_api"
    result = await _discover(hass, _zeroconf(addresses=["fe80::1c2:3ff:fe44:5566"]))
    assert result["reason"] == "no_usable_address"


async def test_zeroconf_prefers_ipv4(hass: HomeAssistant) -> None:
    """IPv4 wins; a routable IPv6 is used only when there's nothing else."""
    result = await _discover(hass, _zeroconf(addresses=["2601:646:8100:1a2b::55", "192.168.1.155"]))
    assert hass.config_entries.flow._progress[result["flow_id"]]._host == "192.168.1.155"  # noqa: SLF001
    hass.config_entries.flow.async_abort(result["flow_id"])

    result = await _discover(hass, _zeroconf(addresses=["fe80::1", "2601:646:8100:1a2b::55"]))
    flow = hass.config_entries.flow._progress[result["flow_id"]]  # noqa: SLF001
    assert flow._host == "2601:646:8100:1a2b::55"  # noqa: SLF001


async def test_user_flow_existing_id_new_host(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    """Typed in again at a new address: abort, and the entry takes the host."""
    entry = make_entry(fake_cue, unique_id=DEVICE_ID)
    entry.add_to_hass(hass)
    entry_host_before = entry.data[CONF_HOST]
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_HOST: "10.9.9.9"})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: entry_host_before, CONF_PORT: fake_cue.port}
    )
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == entry_host_before


# MARK: migration at setup


async def test_setup_migrates_host_keyed_entry(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    """Old build → entities keyed on the host; the app updates; on reload the
    entry, its entities and its device move to the id, entity ids unchanged."""
    fake_cue.legacy()
    entry = make_entry(fake_cue)
    await setup_entry(hass, entry)
    before = _entity_unique_ids(hass, entry.entry_id)
    assert entry.unique_id == "127.0.0.1"
    assert before[PLAYER] == "127.0.0.1"
    assert before["select.living_room_subtitles"] == "127.0.0.1_subtitles"

    fake_cue.info = FakeCue().info  # the Slice B build
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.unique_id == DEVICE_ID
    after = _entity_unique_ids(hass, entry.entry_id)
    assert set(after) == set(before)  # same entity ids
    assert after[PLAYER] == DEVICE_ID
    assert after["select.living_room_subtitles"] == f"{DEVICE_ID}_subtitles"
    assert after["binary_sensor.living_room_cue_open"] == f"{DEVICE_ID}_cue_open"
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert [d.identifiers for d in devices] == [{(DOMAIN, DEVICE_ID)}]
    assert hass.states.get(PLAYER).state == "idle"


async def test_late_info_migrates(
    freezer: FrozenDateTimeFactory, hass: HomeAssistant, fake_cue: FakeCue
) -> None:
    """Cue wasn't open at setup: the first good poll learns the id and reloads."""
    fake_cue.legacy()
    entry = make_entry(fake_cue)
    await setup_entry(hass, entry)  # registers host-keyed entities
    await fake_cue.go_away()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.unique_id == "127.0.0.1"
    assert entry.runtime_data.info is None

    fake_cue.info = FakeCue().info
    await fake_cue.come_back()
    freezer.tick(timedelta(seconds=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert entry.unique_id == DEVICE_ID
    assert _entity_unique_ids(hass, entry.entry_id)[PLAYER] == DEVICE_ID
    assert entry.runtime_data.info.device_id == DEVICE_ID


async def test_id_taken_by_another_entry(hass: HomeAssistant, fake_cue: FakeCue, socket_enabled) -> None:
    """Two entries can't both claim one id: the second keeps its host key."""
    other = make_entry(fake_cue, unique_id=DEVICE_ID)
    other.add_to_hass(hass)
    entry = make_entry(fake_cue)
    await setup_entry(hass, entry)  # sets up both entries
    assert other.unique_id == DEVICE_ID
    assert entry.unique_id == "127.0.0.1"
    ids = _entity_unique_ids(hass, entry.entry_id)
    assert "127.0.0.1" in ids.values()  # its media player kept the host key
    assert DEVICE_ID in _entity_unique_ids(hass, other.entry_id).values()


# MARK: features


async def test_features_shape_entities(hass: HomeAssistant, socket_enabled: None) -> None:
    """A build that lists `stop` and lacks `skip` and `rate`."""
    fake = FakeCue(extra_commands=("stop",))
    await fake.start()
    try:
        fake.info["features"] = [f for f in fake.info["features"] if f not in ("skip", "rate")] + ["stop"]
        fake.play()
        await setup_entry(hass, make_entry(fake))

        assert hass.states.get("button.living_room_skip_intro") is None
        assert hass.states.get("button.living_room_skip_credits") is None
        assert hass.states.get("select.living_room_speed") is None
        assert hass.states.get("button.living_room_restart") is not None
        features = MediaPlayerEntityFeature(hass.states.get(PLAYER).attributes["supported_features"])
        assert MediaPlayerEntityFeature.STOP in features
        assert MediaPlayerEntityFeature.PREVIOUS_TRACK not in features

        await hass.services.async_call(MP_DOMAIN, SERVICE_MEDIA_STOP, {ATTR_ENTITY_ID: PLAYER}, blocking=True)
        assert fake.requests[-1][1] == "/v1/player/stop"
        assert hass.states.get(PLAYER).state == "idle"
    finally:
        await fake.close()


async def test_legacy_build_gets_legacy_features(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    """No `features`: everything a pre-Slice-B build serves, no STOP."""
    fake_cue.legacy()
    fake_cue.play()
    await setup_entry(hass, make_entry(fake_cue))
    features = MediaPlayerEntityFeature(hass.states.get(PLAYER).attributes["supported_features"])
    assert MediaPlayerEntityFeature.STOP not in features
    assert MediaPlayerEntityFeature.SEEK in features
    assert hass.states.get("button.living_room_skip_intro") is not None


async def test_info_sends_token(hass: HomeAssistant, fake_cue: FakeCue, entry) -> None:
    """`/v1/info` carries the token once there is one (rides an address lockout)."""
    info_auth = [h for p, h in fake_cue.auth_headers if p == "/v1/info"]
    assert info_auth == [f"Bearer {TOKEN}"]


# MARK: repair issue


async def test_outdated_issue(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    """An old build raises the issue; a newer one clears it; removal deletes it."""
    fake_cue.legacy()
    entry = make_entry(fake_cue)
    await setup_entry(hass, entry)
    issue_id = f"{ISSUE}_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_key == ISSUE
    assert issue.translation_placeholders == {"name": "Living Room", "version": "1.2"}
    assert issue.severity is ir.IssueSeverity.WARNING

    fake_cue.info = FakeCue().info
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None

    fake_cue.legacy()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_no_issue_for_current_build(hass: HomeAssistant, entry) -> None:
    """A Slice B build raises nothing."""
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE}_{entry.entry_id}") is None
