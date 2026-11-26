"""Slice F (Cue 1.2.1): pairing with a code on the TV, the token as the
fallback, reauth by pairing, and revoking a paired token on removal."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.cue.const import CONF_PAIRED, DOMAIN

from .conftest import make_entry, setup_entry
from .fake_cue import PAIR_CODE, TOKEN, FakeCue


async def _to_menu(hass: HomeAssistant, fake: FakeCue):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: fake.host, CONF_PORT: fake.port}
    )


async def test_pair_with_the_code(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    fake_cue.enable_pairing()
    result = await _to_menu(hass, fake_cue)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["pair", "token"]

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "pair"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "pair"
    assert fake_cue.pairing["name"] == "Home Assistant"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "000000"})
    assert result["errors"] == {"code": "wrong_code"}
    assert "2 tries left" in result["description_placeholders"]["message"]
    assert fake_cue.pair_starts == 1  # the same pairing

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "482 913"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TOKEN] == "paired-token-1"
    assert result["data"][CONF_PAIRED] is True
    await hass.async_block_till_done()
    assert hass.states.get("media_player.living_room") is not None


async def test_expired_pairing_starts_a_fresh_one(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    fake_cue.enable_pairing()
    result = await _to_menu(hass, fake_cue)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "pair"})
    fake_cue.pairing["live"] = False  # 120 s went by
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": PAIR_CODE})
    assert result["errors"] == {"base": "pairing_expired"}
    assert fake_cue.pair_starts == 2
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": PAIR_CODE})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_busy(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    fake_cue.enable_pairing()
    fake_cue.pairing = {"id": "other", "name": "x", "tries": 3, "live": True}
    result = await _to_menu(hass, fake_cue)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "pair"})
    assert result["errors"] == {"base": "pairing_busy"}


async def test_token_still_works(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    fake_cue.enable_pairing()
    result = await _to_menu(hass, fake_cue)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "token"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: TOKEN})
    assert result["type"] is FlowResultType.CREATE_ENTRY and result["data"][CONF_PAIRED] is False


async def test_older_build_goes_straight_to_the_token(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    result = await _to_menu(hass, fake_cue)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "token"


async def test_zeroconf_pair_flag(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    from ipaddress import ip_address

    fake_cue.enable_pairing()
    info = ZeroconfServiceInfo(
        ip_address=ip_address("127.0.0.1"), ip_addresses=[ip_address("127.0.0.1")], port=fake_cue.port,
        hostname="living-room.local.", type="_cue-control._tcp.local.", name="Living Room._cue-control._tcp.local.",
        properties={"api": "1", "id": "new-id", "name": "Living Room", "pair": "1"},
    )
    before = len(fake_cue.requests)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=info)
    assert result["type"] is FlowResultType.MENU
    assert len(fake_cue.requests) == before  # decided from the TXT


async def test_reauth_by_pairing(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    fake_cue.enable_pairing()
    entry = make_entry(fake_cue)
    await setup_entry(hass, entry)
    fake_cue.token = "REGENERATED"
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.MENU and result["menu_options"] == ["pair", "reauth_confirm"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "pair"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": PAIR_CODE})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN] == "paired-token-1" and entry.data[CONF_PAIRED] is True


async def test_removing_a_paired_entry_revokes_it(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    fake_cue.enable_pairing()
    fake_cue.paired_tokens.add("paired-x")
    entry = make_entry(fake_cue, token="paired-x", data={CONF_PAIRED: True})
    await setup_entry(hass, entry)
    await hass.config_entries.async_remove(entry.entry_id)
    assert ("DELETE", "/v1/clients/self", None) in fake_cue.requests
    assert "paired-x" not in fake_cue.paired_tokens


async def test_removing_a_token_entry_leaves_the_token(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    entry = make_entry(fake_cue)
    await setup_entry(hass, entry)
    await hass.config_entries.async_remove(entry.entry_id)
    assert "/v1/clients/self" not in fake_cue.paths()
    assert fake_cue.token == TOKEN
