"""A small client for Cue's `/v1` Control API on an Apple TV.

Contract: `Docs/control-api/openapi.yaml` in the Cue repo (a snapshot lives in
`tests/fixtures/openapi.yaml`). Plain HTTP on the LAN, one request per
connection, bearer token on everything except `GET /v1/info`.

The client never retries. A wrong token counts against Cue's failed-auth
limiter, which is keyed by IP alone, so a retry loop here would lock every
other client on the same host (Ben's rest_commands) out for 5 minutes.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import Any
from urllib.parse import quote, urlencode

import aiohttp

from .const import DEFAULT_RETRY_AFTER, EVENTS_READ_TIMEOUT, LIBRARY_TIMEOUT, REQUEST_TIMEOUT


class CueError(Exception):
    """Base class for Cue client errors."""


class CueConnectionError(CueError):
    """The Apple TV didn't answer: Cue isn't open (tvOS suspends it)."""


class CueResponseError(CueError):
    """Cue answered with a v1 error body."""

    def __init__(
        self,
        status: int,
        code: str | None,
        message: str | None,
        *,
        param: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.message = message or f"HTTP {status}"
        self.param = param
        self.retry_after = retry_after
        super().__init__(f"{status} {code or '?'}: {self.message}")


class CueAuthError(CueResponseError):
    """401: the token is wrong or was regenerated on the TV."""


class CueRateLimitedError(CueResponseError):
    """429: too many wrong tokens from this address; wait `retry_after` s."""


def normalize_token(token: str) -> str:
    """Accept a token pasted with or without a leading `Bearer `."""
    token = token.strip()
    if token[:7].lower() == "bearer ":
        token = token[7:].strip()
    return token


class CueClient:
    """Talks to one Apple TV."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        token: str | None = None,
    ) -> None:
        self._session = session
        self.host = host
        self.port = port
        self._token = normalize_token(token) if token else None
        bracketed = f"[{host}]" if ":" in host and not host.startswith("[") else host
        self.base_url = f"http://{bracketed}:{port}"

    async def get_info(self) -> dict[str, Any]:
        """`GET /v1/info`. Needs no token, but sends one when it has it: from
        Slice B, Cue lets a known-good token through an address lockout,
        unauthenticated calls included."""
        return await self._json("GET", "/v1/info", auth=None)

    async def get_status(self) -> dict[str, Any]:
        """`GET /v1/status`."""
        return await self._json("GET", "/v1/status")

    async def command(
        self, name: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """`POST /v1/player/<name>`. Answers the status after the command."""
        return await self._json("POST", f"/v1/player/{name}", body=body or {})

    async def search(
        self, query: str, types: list[str] | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """`GET /v1/search` (Cue 1.2.1). Spaces go as `%20`: Cue reads `+`
        as a literal plus."""
        params = {"q": query}
        if types:
            params["type"] = ",".join(types)
        if limit:
            params["limit"] = str(limit)
        path = "/v1/search?" + urlencode(params, quote_via=quote)
        return await self._json("GET", path, timeout=LIBRARY_TIMEOUT)

    async def continue_watching(self) -> dict[str, Any]:
        """`GET /v1/hubs/continue` (Cue 1.2.1)."""
        return await self._json("GET", "/v1/hubs/continue", timeout=LIBRARY_TIMEOUT)

    async def open(self, body: dict[str, Any]) -> dict[str, Any]:
        """`POST /v1/open` (Cue 1.2.1): 202 `{requestId}`; the outcome is
        `status.lastOpen`."""
        return await self._json("POST", "/v1/open", body=body, ok=(202,), timeout=LIBRARY_TIMEOUT)

    async def events(self) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """`GET /v1/events` (Cue 1.2.1): `(event, data)` until the stream
        ends. Comments (keepalives) are skipped; a frame whose data isn't a
        JSON object is dropped. Raises like any request on a refusal; a
        stream that goes quiet past two keepalives raises
        CueConnectionError."""
        headers = {"Accept": "text/event-stream"}
        if not self._token:
            raise CueAuthError(401, "unauthorized", "No token configured")
        headers["Authorization"] = f"Bearer {self._token}"
        try:
            async with self._session.get(
                self.base_url + "/v1/events",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=None, connect=REQUEST_TIMEOUT, sock_read=EVENTS_READ_TIMEOUT),
                allow_redirects=False,
            ) as resp:
                if resp.status != 200:
                    raise _error_for(resp.status, resp.headers, await resp.read())
                event, data = "message", []
                async for raw in resp.content:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if not line:
                        if data:
                            try:
                                payload = json.loads("\n".join(data))
                            except ValueError:
                                payload = None
                            if isinstance(payload, dict):
                                yield event, payload
                        event, data = "message", []
                    elif line.startswith(":"):
                        continue
                    else:
                        name, _, value = line.partition(":")
                        value = value[1:] if value.startswith(" ") else value
                        if name == "event":
                            event = value
                        elif name == "data":
                            data.append(value)
        except (TimeoutError, aiohttp.ClientError, OSError) as err:
            raise CueConnectionError(f"Cue's event stream ended: {err!r}") from err

    async def get_artwork(self, path: str) -> tuple[bytes, str]:
        """Fetch an artwork path from status (`/v1/artwork/still?key=…`)."""
        if not path.startswith("/v1/artwork/"):
            raise CueError(f"Not an artwork path: {path}")
        status, headers, body = await self._request("GET", path)
        if status != 200:
            raise _error_for(status, headers, body)
        return body, headers.get("Content-Type", "image/jpeg")

    async def _json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        auth: bool | None = True,
        ok: tuple[int, ...] = (200,),
        timeout: float = REQUEST_TIMEOUT,
    ) -> dict[str, Any]:
        status, headers, raw = await self._request(method, path, body=body, auth=auth, timeout=timeout)
        if status not in ok:
            raise _error_for(status, headers, raw)
        try:
            data = json.loads(raw)
        except ValueError as err:
            raise CueResponseError(status, None, "Cue sent a reply that isn't JSON") from err
        if not isinstance(data, dict):
            raise CueResponseError(status, None, "Cue sent a reply that isn't an object")
        return data

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        auth: bool | None = True,
        timeout: float = REQUEST_TIMEOUT,
    ) -> tuple[int, Mapping[str, str], bytes]:
        """`auth`: True requires the token, None sends it if there is one."""
        headers = {"Accept": "application/json"}
        if auth and not self._token:
            raise CueAuthError(401, "unauthorized", "No token configured")
        if auth is not False and self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            async with self._session.request(
                method,
                self.base_url + path,
                json=body if method == "POST" else None,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=False,
            ) as resp:
                raw = await resp.read()
                return resp.status, resp.headers.copy(), raw
        except (TimeoutError, aiohttp.ClientError, OSError) as err:
            raise CueConnectionError(
                f"Couldn't reach Cue at {self.base_url}: {err!r}"
            ) from err


def _error_for(status: int, headers: Mapping[str, str], raw: bytes) -> CueResponseError:
    code: str | None = None
    message: str | None = None
    param: str | None = None
    try:
        detail = json.loads(raw).get("error")
        if isinstance(detail, dict):
            code = detail.get("code")
            message = detail.get("message")
            param = detail.get("param")
    except (ValueError, AttributeError):
        pass

    if status == 401:
        return CueAuthError(status, code or "unauthorized", message)
    if status == 429:
        retry_after = DEFAULT_RETRY_AFTER
        try:
            retry_after = max(1, int(headers.get("Retry-After", "")))
        except ValueError:
            pass
        return CueRateLimitedError(
            status, code or "rate_limited", message, retry_after=retry_after
        )
    return CueResponseError(status, code, message, param=param)
