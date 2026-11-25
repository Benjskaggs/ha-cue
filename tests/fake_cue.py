"""A fake Cue Control API v1, built from `fixtures/openapi.yaml`'s shapes.

It is a real aiohttp server on 127.0.0.1, so the integration's client runs
unmodified. It mirrors the server's gate order (`ControlServer.swift`):
rate limit → disabled → token. Its failed-auth limiter is deliberately the
strict pre-Slice-B one (any 10 wrong tokens a minute lock the whole address
for 5 minutes): the worst case, so tests prove the integration never retries
a bad token whatever build it meets.

By default it's a Slice B build (`/v1/info` has `id` and `features`);
`legacy()` turns it into an older one.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
import socket
from typing import Any

from aiohttp import web

TOKEN = "K7QM-2XRA-PLMN-4TQZ"
SERVER_ID = "0123abcd"
JPEG = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"

APP = {"version": "1.2", "build": "2026.1125.900", "platform": "tvOS"}

DEVICE_ID = "3f2a5c1e-8b7d-4e6f-9a0b-1c2d3e4f5a6b"
FEATURES = ["info", "status", "artwork", "play", "pause", "toggle", "restart", "next",
            "seek", "rate", "subtitles", "audio", "skip"]
INFO = {"api": 1, "id": DEVICE_ID, "name": "Living Room", "app": APP, "pairing": False,
        "features": FEATURES}

IDLE = {
    "api": 1,
    "app": APP,
    "player": None,
    "onstage": {"enabled": False, "available": True},
}


def now_iso() -> str:
    """Like Cue's encoder: internet date-time with fractional seconds."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def playing_status(rating_key: str = "12345") -> dict[str, Any]:
    """The openapi `playing` example, with a second subtitle track and a
    credits marker so every select and button has something to do."""
    return {
        "api": 1,
        "app": {**APP, "server": {"id": SERVER_ID}},
        "player": {
            "state": "playing",
            "positionMs": 1155000,
            "positionAt": now_iso(),
            "rate": 1.0,
            "durationMs": 2583000,
            "item": {
                "ratingKey": rating_key,
                "type": "episode",
                "title": "Before and After",
                "year": 2009,
                "server": SERVER_ID,
                "show": {"title": "Grey's Anatomy", "ratingKey": "100"},
                "season": 5,
                "episode": 15,
            },
            "subtitles": {
                "selected": "3",
                "tracks": [
                    {"id": "3", "title": "English (SRT)", "language": "en", "system": False, "selected": True},
                    {"id": "4", "title": "Spanish (SRT)", "language": "es", "system": False, "selected": False},
                ],
            },
            "audio": {
                "selected": "1",
                "tracks": [
                    {"id": "1", "title": "English 5.1", "language": "en", "selected": True},
                    {"id": "2", "title": "Commentary", "language": "en", "selected": False},
                ],
            },
            "markers": [
                {"type": "intro", "startMs": 60000, "endMs": 95000, "active": False, "skippable": False},
                {"type": "credits", "startMs": 2500000, "endMs": 2583000, "active": False, "skippable": False},
            ],
            "queue": {"next": {"ratingKey": "12346", "title": "Rise Up", "season": 5, "episode": 16}},
            "zoom": "fit",
            "artwork": {
                "still": f"/v1/artwork/still?key={rating_key}",
                "art": f"/v1/artwork/art?key={rating_key}",
                "poster": f"/v1/artwork/poster?key={rating_key}",
            },
        },
        "onstage": {"enabled": False, "available": True},
    }


def _error(status: int, code: str, message: str, **headers: str) -> web.Response:
    return web.json_response(
        {"error": {"code": code, "message": message, "status": status}},
        status=status,
        headers=headers or None,
    )


def _canon(token: str) -> str:
    return token.replace("-", "").upper()


class FakeCue:
    """One fake Apple TV."""

    COMMANDS = ("play", "pause", "toggle", "restart", "next", "seek", "rate", "subtitles", "audio", "skip")

    def __init__(self, extra_commands: tuple[str, ...] = ()) -> None:
        self.extra_commands = extra_commands
        self.token = TOKEN
        self.status: dict[str, Any] = copy.deepcopy(IDLE)
        self.info: dict[str, Any] = copy.deepcopy(INFO)
        self.disabled = False
        self.not_lan = False
        self.locked_retry_after: int | None = None
        self.failed_auth = 0
        # (method, path, body) for every request that reached a handler.
        self.requests: list[tuple[str, str, Any]] = []
        # (path, Authorization header or None), same order.
        self.auth_headers: list[tuple[str, str | None]] = []
        self.host = "127.0.0.1"
        self.port = 0  # picked on first start()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    # MARK: lifecycle (a stopped site refuses connections: "Cue isn't open")

    async def start(self) -> None:
        if not self.port:
            self.port = _free_port()
        if self._runner is None:
            self._runner = web.AppRunner(self.app(), access_log=None)
            await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port, reuse_address=True)
        await self._site.start()

    async def go_away(self) -> None:
        """tvOS suspended Cue: the listener is gone."""
        if self._site is not None:
            await self._site.stop()
            self._site = None

    async def come_back(self) -> None:
        await self.start()

    async def close(self) -> None:
        await self.go_away()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # MARK: state helpers for tests

    def play(self, rating_key: str = "12345") -> None:
        self.status = playing_status(rating_key)

    def idle(self) -> None:
        self.status = copy.deepcopy(IDLE)

    def legacy(self) -> None:
        """A build before Slice B: no `id`, no `features`."""
        self.info.pop("id", None)
        self.info.pop("features", None)

    @property
    def player(self) -> dict[str, Any] | None:
        return self.status["player"]

    def paths(self, method: str | None = None) -> list[str]:
        return [p for m, p, _ in self.requests if method is None or m == method]

    def count(self, path: str) -> int:
        return sum(1 for _, p, _ in self.requests if p == path)

    # MARK: the app

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._gates])
        app.router.add_get("/v1/info", self._info)
        app.router.add_get("/v1/status", self._status)
        app.router.add_get("/v1/artwork/{kind}", self._artwork)
        for name in (*self.COMMANDS, *self.extra_commands):
            app.router.add_post(f"/v1/player/{name}", self._command)
        return app

    @web.middleware
    async def _gates(self, request: web.Request, handler):
        body: Any = None
        if request.method == "POST" and request.can_read_body:
            raw = await request.read()
            if raw.strip():
                try:
                    body = await request.json()
                except ValueError:
                    response = _error(400, "bad_request", "The body must be a JSON object.")
                    response.force_close()
                    return response
        self.requests.append((request.method, request.path, body))
        self.auth_headers.append((request.path, request.headers.get("Authorization")))
        request["body"] = body if body is not None else {}
        response = await self._gated(request, handler)
        response.force_close()  # Cue answers one request per connection
        return response

    async def _gated(self, request: web.Request, handler) -> web.StreamResponse:

        if self.not_lan:
            return _error(403, "not_lan", "Only devices on this Apple TV's local network can use the Control API.")
        if self.locked_retry_after is not None:
            return _error(429, "rate_limited", "Too many wrong tokens.", **{"Retry-After": str(self.locked_retry_after)})
        if self.disabled:
            return _error(503, "disabled", "Home Assistant Control is turned off on this Apple TV.")
        if request.path != "/v1/info":
            auth = request.headers.get("Authorization", "")
            if not (auth.startswith("Bearer ") and _canon(auth[7:]) == _canon(self.token)):
                self.failed_auth += 1
                if self.failed_auth >= 10:
                    self.locked_retry_after = 300
                return _error(401, "unauthorized", "Missing or wrong token.")
        return await handler(request)

    async def _info(self, request: web.Request) -> web.Response:
        return web.json_response(self.info)

    async def _status(self, request: web.Request) -> web.Response:
        if self.player:
            self.player["positionAt"] = now_iso()
        return web.json_response(self.status)

    async def _artwork(self, request: web.Request) -> web.Response:
        kind = request.match_info["kind"]
        if kind not in ("still", "art", "poster"):
            return _error(404, "not_found", "Artwork kinds are still, art and poster.")
        if not self.player:
            return _error(409, "nothing_playing", "Nothing is playing.")
        if kind not in self.player["artwork"]:
            return _error(404, "not_found", f"This item has no {kind} artwork.")
        return web.Response(body=JPEG, content_type="image/jpeg")

    async def _command(self, request: web.Request) -> web.Response:
        name = request.path.rsplit("/", 1)[1]
        body: dict[str, Any] = request["body"]
        p = self.player
        # Validation first (a malformed body is a 400 even when idle).
        if name == "seek":
            if ("positionMs" in body) == ("deltaMs" in body):
                return _error(400, "invalid_parameter", "Send positionMs or deltaMs.")
            value = body.get("positionMs", body.get("deltaMs"))
            if not isinstance(value, int) or isinstance(value, bool):
                return _error(400, "invalid_parameter", "Whole milliseconds.")
        if name == "rate":
            rate = body.get("rate")
            if not isinstance(rate, (int, float)) or isinstance(rate, bool) or not 0 < rate <= 4:
                return _error(400, "invalid_parameter", "rate must be in (0, 4].")
        if name in ("subtitles", "audio") and "id" not in body:
            return _error(400, "invalid_parameter", f"{name} needs an id.")
        if name == "skip" and body.get("marker") not in ("intro", "credits"):
            return _error(400, "invalid_parameter", "marker must be intro or credits.")
        if p is None:
            return _error(409, "nothing_playing", "Nothing is playing.")

        if name == "play":
            p["state"] = "playing"
        elif name == "pause":
            p["state"] = "paused"
        elif name == "toggle":
            p["state"] = "paused" if p["state"] == "playing" else "playing"
        elif name == "restart":
            p["positionMs"] = 0
        elif name == "stop":
            self.idle()
            return web.json_response(self.status)
        elif name == "next":
            nxt = p["queue"].get("next")
            if not nxt:
                return _error(409, "no_next", "There's no next episode.")
            self.play(nxt["ratingKey"])
            p = self.player
            p["item"].update(title=nxt["title"], season=nxt.get("season"), episode=nxt.get("episode"))
            p["queue"] = {}
            p["positionMs"] = 0
        elif name == "seek":
            target = body["positionMs"] if "positionMs" in body else p["positionMs"] + body["deltaMs"]
            p["positionMs"] = max(0, min(target, p["durationMs"]))
        elif name == "rate":
            p["rate"] = float(body["rate"])
        elif name in ("subtitles", "audio"):
            wanted = body["id"]
            block = p[name]
            if wanted is None and name == "subtitles":
                block["selected"] = None
            elif not any(t["id"] == wanted for t in block["tracks"]):
                return _error(404, "unknown_track", f"No {name} track {wanted}.")
            else:
                block["selected"] = wanted
            for track in block["tracks"]:
                track["selected"] = track["id"] == block["selected"]
        elif name == "skip":
            marker = next(
                (m for m in p["markers"] if m["type"] == body["marker"] and m["endMs"] > p["positionMs"]),
                None,
            )
            if marker is None:
                return _error(409, "no_marker", "No marker ahead.")
            p["positionMs"] = marker["endMs"]
            marker["active"] = marker["skippable"] = False

        p["positionAt"] = now_iso()
        return web.json_response(self.status)

    def set_marker(self, kind: str, *, skippable: bool) -> None:
        for marker in self.player["markers"]:
            if marker["type"] == kind:
                marker["active"] = marker["skippable"] = skippable
                if skippable:
                    self.player["positionMs"] = marker["startMs"] + 1000


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
