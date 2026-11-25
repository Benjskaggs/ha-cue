"""Config, reauth and options flows."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, CONF_SCAN_INTERVAL, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.cue.const import CONF_APPLE_TV_ENTITY, DOMAIN

from .conftest import ATV_ENTITY, FakeAppleTV, make_entry, setup_entry
from .fake_cue import TOKEN, FakeCue, _free_port


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def test_full_flow(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    """Host → /v1/info, token → /v1/status, entry created with a clean token."""
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: fake_cue.host, CONF_PORT: fake_cue.port}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "token"
    assert result["description_placeholders"]["name"] == "Living Room"
    assert fake_cue.paths() == ["/v1/info"]

    # Pasted the way Ben's secrets.yaml holds it: with "Bearer ", lower-case.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOKEN: f"  Bearer {TOKEN.lower()} "}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Living Room"
    assert result["data"] == {
        CONF_HOST: fake_cue.host,
        CONF_PORT: fake_cue.port,
        CONF_TOKEN: TOKEN.lower(),
        CONF_NAME: "Living Room",
        "app_version": "1.2",
    }
    assert result["result"].unique_id == fake_cue.host
    assert fake_cue.paths()[:2] == ["/v1/info", "/v1/status"]


async def test_cannot_connect(hass: HomeAssistant, socket_enabled: None) -> None:
    """Nothing listening: Cue isn't open."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: _free_port()}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_info_errors(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    """403 not_lan, 503 disabled, 429 with Retry-After, and a non-Cue reply."""
    result = await _start(hass)
    user = {CONF_HOST: fake_cue.host, CONF_PORT: fake_cue.port}

    fake_cue.not_lan = True
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user)
    assert result["errors"] == {"base": "not_lan"}

    fake_cue.not_lan = False
    fake_cue.disabled = True
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user)
    assert result["errors"] == {"base": "disabled"}

    fake_cue.disabled = False
    fake_cue.locked_retry_after = 120
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user)
    assert result["errors"] == {"base": "rate_limited"}
    assert result["description_placeholders"]["retry_after"] == "120"

    fake_cue.locked_retry_after = None
    fake_cue.info["api"] = 2
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user)
    assert result["errors"] == {"base": "not_cue"}

    fake_cue.info["api"] = 1
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user)
    assert result["step_id"] == "token"


async def test_bad_token_tried_once(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    """A wrong token is one request and one limiter strike, then a form."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: fake_cue.host, CONF_PORT: fake_cue.port}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOKEN: "WRONG-TOKEN"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert fake_cue.failed_auth == 1
    assert fake_cue.count("/v1/status") == 1

    fake_cue.locked_retry_after = 300
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOKEN: TOKEN}
    )
    assert result["errors"] == {"base": "rate_limited"}
    assert result["description_placeholders"]["retry_after"] == "300"

    fake_cue.locked_retry_after = None
    fake_cue.disabled = True
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOKEN: TOKEN}
    )
    assert result["errors"] == {"base": "disabled"}

    fake_cue.disabled = False
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOKEN: TOKEN}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_already_configured(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    """The same host twice aborts before any request."""
    make_entry(fake_cue).add_to_hass(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: fake_cue.host, CONF_PORT: fake_cue.port}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert fake_cue.requests == []


async def test_reauth(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    """A regenerated token on the TV: reauth, then the entry reloads and polls."""
    fake_cue.play()
    entry = make_entry(fake_cue, token="OLD-TOKEN")
    await setup_entry(hass, entry)
    assert entry.state is config_entries.ConfigEntryState.SETUP_ERROR

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    flow = flows[0]
    assert flow["context"]["source"] == config_entries.SOURCE_REAUTH
    assert flow["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_TOKEN: "STILL-WRONG"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_TOKEN: TOKEN}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()

    assert entry.data[CONF_TOKEN] == TOKEN
    assert entry.state is config_entries.ConfigEntryState.LOADED
    assert hass.states.get("media_player.living_room").state == "playing"
    # Setup (1) + the wrong reauth try (1); nothing retried behind the user.
    assert fake_cue.failed_auth == 2


async def test_options(
    hass: HomeAssistant, fake_cue: FakeCue, apple_tv: FakeAppleTV
) -> None:
    """Poll interval and the linked apple_tv entity; the entry reloads."""
    entry = make_entry(fake_cue)
    await setup_entry(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 8, CONF_APPLE_TV_ENTITY: ATV_ENTITY}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options == {CONF_SCAN_INTERVAL: 8, CONF_APPLE_TV_ENTITY: ATV_ENTITY}
    assert entry.runtime_data.update_interval.total_seconds() == 8

    # Unlinking drops the key.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 5}
    )
    await hass.async_block_till_done()
    assert entry.options == {CONF_SCAN_INTERVAL: 5}
