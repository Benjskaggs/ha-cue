"""Config flow for Cue: host, then token; reauth; options."""

from __future__ import annotations

from collections.abc import Mapping
from ipaddress import IPv4Address, IPv6Address
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, CONF_SCAN_INTERVAL, CONF_TOKEN
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .api import (
    CueAuthError,
    CueClient,
    CueConnectionError,
    CueError,
    CueRateLimitedError,
    CueResponseError,
    normalize_token,
)
from .const import (
    CONF_APP_VERSION,
    CONF_APPLE_TV_ENTITY,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .coordinator import CueConfigEntry

_LOGGER = logging.getLogger(__name__)

TOKEN_SCHEMA = vol.Schema(
    {vol.Required(CONF_TOKEN): selector.TextSelector(
        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
    )}
)


def _error_key(err: CueError) -> str:
    """Map a client error to a `config.error.*` key."""
    if isinstance(err, CueConnectionError):
        return "cannot_connect"
    if isinstance(err, CueAuthError):
        return "invalid_auth"
    if isinstance(err, CueRateLimitedError):
        return "rate_limited"
    if isinstance(err, CueResponseError):
        if err.code == "not_lan":
            return "not_lan"
        if err.code == "disabled":
            return "disabled"
        if err.code == "signed_out":
            return "signed_out"
    return "unknown"


def _pick_host(info: ZeroconfServiceInfo) -> str | None:
    """IPv4 first (what Cue's peer check has always accepted), then a
    routable IPv6. Never a link-local IPv6: it needs a scope id HA can't use."""
    addresses = list(info.ip_addresses) or [info.ip_address]
    for address in addresses:
        if isinstance(address, IPv4Address):
            return str(address)
    for address in addresses:
        if isinstance(address, IPv6Address) and not (address.is_link_local or address.is_loopback):
            return str(address)
    return None


class CueConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add an Apple TV running Cue."""

    VERSION = 1

    def __init__(self) -> None:
        self._host: str | None = None
        self._port: int = DEFAULT_PORT
        self._info: dict[str, Any] = {}
        self._placeholders: dict[str, str] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: CueConfigEntry) -> CueOptionsFlow:
        """Options: poll interval and the linked apple_tv entity."""
        return CueOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: where the Apple TV is. Checks `GET /v1/info` (no token),
        and keys the entry on its stable id when the build has one."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = int(user_input.get(CONF_PORT, DEFAULT_PORT))
            client = CueClient(async_get_clientsession(self.hass), host, port)
            try:
                info = await client.get_info()
            except CueError as err:
                errors["base"] = _error_key(err)
                self._note_retry_after(err)
            else:
                if info.get("api") != 1:
                    errors["base"] = "not_cue"
                else:
                    device_id = info.get("id") if isinstance(info.get("id"), str) else None
                    if abort := await self._async_claim(host, port, device_id):
                        return abort
                    self._host, self._port, self._info = host, port, info
                    return await self.async_step_token()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_HOST): str,
                        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(
                            vol.Coerce(int), vol.Range(min=1, max=65535)
                        ),
                    }
                ),
                user_input,
            ),
            errors=errors,
            description_placeholders=self._placeholders,
        )

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Found `_cue-control._tcp` on the LAN (only while Cue is frontmost:
        tvOS suspends it, and the advertisement, in the background)."""
        props = discovery_info.properties
        api = props.get("api") or props.get("v")
        if api is not None and str(api) != "1":
            return self.async_abort(reason="unsupported_api")
        host = _pick_host(discovery_info)
        if host is None:
            return self.async_abort(reason="no_usable_address")
        port = discovery_info.port or DEFAULT_PORT
        device_id = props.get("id") or None
        name = props.get("name") or discovery_info.name.split(".")[0] or DEFAULT_NAME

        if abort := await self._async_claim(host, port, device_id):
            return abort
        self._host, self._port = host, port
        self._info = {"name": name, "id": device_id, "app": {"version": props.get("app")}}
        self.context["title_placeholders"] = {"name": name}
        return await self.async_step_token()

    async def _async_claim(
        self, host: str, port: int, device_id: str | None
    ) -> ConfigFlowResult | None:
        """Set this flow's unique_id, or abort for an Apple TV already added.

        A Slice B build has a stable `id`: that's the unique_id, and an entry
        that already has it gets the current host and port (an IP change).
        An entry added before Cue had ids is keyed on its host: it adopts the
        id here (its entities are renamed on the reload). Older builds have
        no id, so the host stays the key.
        """
        if not device_id:
            await self.async_set_unique_id(host)
            self._abort_if_unique_id_configured(updates={CONF_PORT: port})
            return None
        await self.async_set_unique_id(device_id)
        self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_PORT: port})
        legacy = self.hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, host)
        if legacy is not None:
            return self.async_update_reload_and_abort(
                legacy,
                unique_id=device_id,
                data_updates={CONF_HOST: host, CONF_PORT: port},
                reason="already_configured",
            )
        return None

    async def async_step_token(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: the token. Checks `GET /v1/status`."""
        assert self._host is not None
        name = self._info.get("name") or DEFAULT_NAME
        errors: dict[str, str] = {}
        if user_input is not None:
            token = normalize_token(user_input[CONF_TOKEN])
            error = await self._validate_token(self._host, self._port, token)
            if error is None:
                return self.async_create_entry(
                    title=name,
                    data={
                        CONF_HOST: self._host,
                        CONF_PORT: self._port,
                        CONF_TOKEN: token,
                        CONF_NAME: name,
                        CONF_APP_VERSION: (self._info.get("app") or {}).get("version"),
                    },
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="token",
            data_schema=TOKEN_SCHEMA,
            errors=errors,
            description_placeholders={"name": name, **self._placeholders},
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Cue answered 401: the token changed on the TV."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the new token, check it once, reload."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            token = normalize_token(user_input[CONF_TOKEN])
            error = await self._validate_token(
                entry.data[CONF_HOST], entry.data.get(CONF_PORT, DEFAULT_PORT), token
            )
            if error is None:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_TOKEN: token}
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=TOKEN_SCHEMA,
            errors=errors,
            description_placeholders={"name": entry.title, **self._placeholders},
        )

    async def _validate_token(self, host: str, port: int, token: str) -> str | None:
        """One authenticated request. Returns an error key, or None if good.

        Exactly one attempt per submit: wrong tokens count against Cue's
        per-IP limiter (10 a minute locks the address out for 5 minutes).
        """
        self._placeholders = {}
        if not token:
            return "invalid_auth"
        client = CueClient(async_get_clientsession(self.hass), host, port, token)
        try:
            await client.get_status()
        except CueError as err:
            self._note_retry_after(err)
            return _error_key(err)
        except Exception:
            _LOGGER.exception("Unexpected error checking the Cue token")
            return "unknown"
        return None

    def _note_retry_after(self, err: CueError) -> None:
        if isinstance(err, CueRateLimitedError):
            self._placeholders = {"retry_after": str(err.retry_after)}
        else:
            self._placeholders = {}


class CueOptionsFlow(OptionsFlow):
    """Poll interval and the linked `apple_tv` media player."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The only options step."""
        if user_input is not None:
            data = {CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])}
            if user_input.get(CONF_APPLE_TV_ENTITY):
                data[CONF_APPLE_TV_ENTITY] = user_input[CONF_APPLE_TV_ENTITY]
            return self.async_create_entry(data=data)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        max=MAX_SCAN_INTERVAL,
                        step=1,
                        unit_of_measurement="s",
                        mode=selector.NumberSelectorMode.SLIDER,
                    )
                ),
                vol.Optional(
                    CONF_APPLE_TV_ENTITY,
                    description={"suggested_value": options.get(CONF_APPLE_TV_ENTITY)},
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="media_player", integration="apple_tv"
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
