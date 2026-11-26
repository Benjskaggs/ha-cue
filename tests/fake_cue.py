"""A fake Cue Control API v1, built from `fixtures/openapi.yaml`'s shapes.

It is a real aiohttp server on 127.0.0.1, so the integration's client runs
unmodified. It mirrors the server's gate order (`ControlServer.swift`):
rate limit → disabled → token. Its failed-auth limiter is deliberately the
strict pre-Slice-B one (any 10 wrong tokens a minute lock the whole address
for 5 minutes): the worst case, so tests prove the integration never retries
a bad token whatever build it meets.

By default it's the newest build: Slice B (`/v1/info` `id` + `features`),
the couch-card art (`artwork.logo`, `artwork.square`, `colors`, per
`cue-art-contract.md` §1) and Slice C (stop, previous, subtitle offset,
language bodies, `status.app`). `before_slice_c()` makes it a 1.2 build;
`without_art()` a plain Slice B build; `legacy()` one from before Slice B.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import copy
import json
from datetime import UTC, datetime
import socket
import struct
from typing import Any
import zlib

from aiohttp import web

TOKEN = "K7QM-2XRA-PLMN-4TQZ"
SERVER_ID = "0123abcd"
JPEG = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"

APP = {"version": "1.2", "build": "2026.1125.900", "platform": "tvOS"}

DEVICE_ID = "3f2a5c1e-8b7d-4e6f-9a0b-1c2d3e4f5a6b"
FEATURES = ["info", "status", "artwork", "play", "pause", "toggle", "restart", "next",
            "seek", "rate", "subtitles", "audio", "skip"]
ART_FEATURES = ["artwork.logo", "artwork.square", "colors"]
# Slice C (1.2.1): routes, then capabilities.
C_ROUTES = ["stop", "previous", "subtitleOffset"]
C_CAPABILITIES = ["subtitles.language", "subtitles.enabled", "audio.language", "status.app"]
C_FEATURES = C_ROUTES + C_CAPABILITIES
# Slice D (1.2.1): search, open, Continue Watching.
D_ROUTES = ["search", "open", "continueWatching"]
# Slice E (1.2.1): push. Off in the fake unless a test calls
# `enable_events()`: a stream outlives a test that doesn't expect one.
E_ROUTES = ["events"]
ACCENT = "#E0A030"


def png_with_alpha(rating_key: str) -> bytes:
    """A real 2x1 RGBA PNG: one opaque pixel, one fully transparent, with
    the ratingKey in a tEXt chunk so each item's logo differs."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = b"\x00" + bytes([255, 0, 0, 255, 0, 0, 0, 0])
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 6, 0, 0, 0))
            + chunk(b"tEXt", b"ratingKey\x00" + rating_key.encode())
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))
INFO = {"api": 1, "id": DEVICE_ID, "name": "Living Room", "app": APP, "pairing": False,
        "features": FEATURES + C_ROUTES + D_ROUTES + ART_FEATURES + C_CAPABILITIES}

# The fake's Plex library (openapi `MediaItem`s).
SHOW = {"ratingKey": "100", "type": "show", "title": "Grey's Anatomy", "year": 2005, "server": SERVER_ID}
EPISODE = {"ratingKey": "12345", "type": "episode", "title": "Before and After", "server": SERVER_ID,
           "show": {"title": "Grey's Anatomy", "ratingKey": "100"}, "season": 5, "episode": 15,
           "durationMs": 2583000}
MOVIE = {"ratingKey": "4321", "type": "movie", "title": "The Matrix", "year": 1999, "server": SERVER_ID,
         "durationMs": 8160000, "watched": False}
CATALOG = [SHOW, EPISODE, MOVIE]
CONTINUE = [{**EPISODE, "viewOffsetMs": 1155000, "watched": False}, {**MOVIE, "viewOffsetMs": 60000}]

IDLE = {
    "api": 1,
    "app": APP,
    "player": None,
    "onstage": {"enabled": False, "available": True},
}


def now_iso() -> str:
    """Like Cue's encoder: internet date-time with fractional seconds."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def playing_status(rating_key: str = "12345", *, art: bool = True) -> dict[str, Any]:
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
            "queue": {
                "previous": {"ratingKey": "12344", "title": "Stairway to Heaven", "season": 5, "episode": 14},
                "next": {"ratingKey": "12346", "title": "Rise Up", "season": 5, "episode": 16},
            },
            "zoom": "fit",
            "artwork": {
                "still": f"/v1/artwork/still?key={rating_key}",
                "art": f"/v1/artwork/art?key={rating_key}",
                "poster": f"/v1/artwork/poster?key={rating_key}",
                **({
                    "square": f"/v1/artwork/square?key={rating_key}",
                    "logo": f"/v1/artwork/logo?key={rating_key}",
                } if art else {}),
            },
            **({"colors": {"accent": ACCENT}} if art else {}),
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

    COMMANDS = ("play", "pause", "toggle", "restart", "next", "seek", "rate", "subtitles", "audio", "skip",
                "stop", "previous", "subtitle-offset")

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
        self.art = True
        # Slice C state: what `status.app` and `subtitles.offsetMs` report.
        self.foreground = True
        self.subtitle_offset_ms = 0
        # Slice D: whether /v1/open brings a player up, and its record.
        self.open_starts = True
        # Set: the result /v1/open's record lands on instead ("expired", …).
        self.open_outcome: str | None = None
        self.last_open: dict[str, Any] | None = None
        self.signed_in = True
        # Slice E: one queue per open event stream (None ends it).
        self._streams: list[asyncio.Queue[bytes | None]] = []
        self.refuse_streams: tuple[int, str] | None = None
        # Called inside an artwork request, before it answers (tests use it
        # to change the item mid-fetch).
        self.on_artwork: Callable[[], None] | None = None
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
        self.end_streams()
        if self._site is not None:
            await self._site.stop()
            self._site = None

    async def come_back(self) -> None:
        await self.start()

    async def close(self) -> None:
        self.end_streams()
        await asyncio.sleep(0)
        await self.go_away()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # MARK: state helpers for tests

    def play(self, rating_key: str = "12345") -> None:
        self.status = playing_status(rating_key, art=self.art)
        self.subtitle_offset_ms = 0

    def has(self, feature: str) -> bool:
        return feature in self.info.get("features", FEATURES)

    def drop_features(self, names: list[str]) -> None:
        if "features" in self.info:
            self.info["features"] = [f for f in self.info["features"] if f not in names]

    def before_slice_c(self) -> None:
        """A 1.2 build: art, but nothing from 1.2.1."""
        self.drop_features(C_FEATURES + D_ROUTES)

    def enable_events(self) -> None:
        if "features" in self.info and "events" not in self.info["features"]:
            self.info["features"].append("events")

    @property
    def stream_count(self) -> int:
        return len(self._streams)

    def push(self, event: str, data: dict[str, Any]) -> None:
        frame = f"event: {event}\nid: 1\ndata: {json.dumps(data)}\n\n".encode()
        for queue in self._streams:
            queue.put_nowait(frame)

    def push_status(self) -> None:
        self.push("status", self.snapshot())

    def push_raw(self, raw: bytes) -> None:
        for queue in self._streams:
            queue.put_nowait(raw)

    def end_streams(self) -> None:
        for queue in self._streams:
            queue.put_nowait(None)

    def before_slice_d(self) -> None:
        """A 1.2.1 build with slice C only: no search, open or Continue Watching."""
        self.drop_features(D_ROUTES)

    def idle(self) -> None:
        self.status = copy.deepcopy(IDLE)

    def without_art(self) -> None:
        """A Slice B build from before the couch-card art (and so before
        everything after it)."""
        self.art = False
        self.drop_features(ART_FEATURES + C_FEATURES + D_ROUTES)
        if self.player:
            self.player.pop("colors", None)
            self.player["artwork"].pop("logo", None)
            self.player["artwork"].pop("square", None)

    def legacy(self) -> None:
        """A build before Slice B: no `id`, no `features`, no art extras."""
        self.without_art()
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
        app.router.add_get("/v1/search", self._search)
        app.router.add_get("/v1/hubs/continue", self._continue)
        app.router.add_post("/v1/open", self._open)
        app.router.add_get("/v1/events", self._events)
        return app

    # MARK: events (slice E)

    async def _events(self, request: web.Request) -> web.StreamResponse:
        if not self.has("events"):
            return _error(404, "not_found", "No route /v1/events.")
        if self.refuse_streams:
            status, code = self.refuse_streams
            return _error(status, code, "Refused.")
        response = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream; charset=utf-8"})
        await response.prepare(request)
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._streams.append(queue)
        try:
            await response.write(b": connected\n\n")
            await response.write(f"event: status\nid: 1\ndata: {json.dumps(self.snapshot())}\n\n".encode())
            while (frame := await queue.get()) is not None:
                await response.write(frame)
        finally:
            self._streams.remove(queue)
        return response

    # MARK: library (slice D)

    def _signed_out(self) -> web.Response | None:
        if not self.signed_in:
            return _error(503, "signed_out", "Cue isn't signed in to a Plex server right now.")
        return None

    async def _search(self, request: web.Request) -> web.Response:
        q = request.query.get("q", "").strip()
        if not q:
            return _error(400, "invalid_parameter", "q is the text to search for.")
        types = [t for t in request.query.get("type", "").split(",") if t] or ["movie", "show", "episode"]
        limit = int(request.query.get("limit", "10"))
        if (refused := self._signed_out()) is not None:
            return refused
        needle = q.casefold()
        found = [
            i for i in CATALOG
            if i["type"] in types
            and (needle in i["title"].casefold() or needle in (i.get("show") or {}).get("title", "").casefold())
        ]
        return web.json_response({"query": q, "results": copy.deepcopy(found[:limit])})

    async def _continue(self, request: web.Request) -> web.Response:
        if (refused := self._signed_out()) is not None:
            return refused
        return web.json_response({"items": copy.deepcopy(CONTINUE)})

    async def _open(self, request: web.Request) -> web.Response:
        body: dict[str, Any] = request["body"]
        key = body.get("ratingKey")
        if not isinstance(key, str) or not key.isdigit():
            return _error(400, "invalid_parameter", "ratingKey is a string of digits.")
        if (refused := self._signed_out()) is not None:
            return refused
        if body.get("server") not in (None, SERVER_ID):
            return _error(409, "unsupported", "That item is on another Plex server.")
        item = next((i for i in CATALOG if i["ratingKey"] == key), None)
        if item is None:
            return _error(404, "not_found", "Plex has no such item.")
        self.open_count = getattr(self, "open_count", 0) + 1
        request_id = f"req-{self.open_count}"
        play = body.get("play", True)
        self.last_open = {"requestId": request_id, "ratingKey": key, "result": "pending" if play else "opened"}
        if play and self.open_outcome:
            self.last_open["result"] = self.open_outcome
        elif play and self.open_starts:
            # A show resumes into its half-watched episode.
            started = EPISODE["ratingKey"] if item["type"] == "show" else key
            self.play(started)
            self.player["item"].update(title=next(i["title"] for i in CATALOG if i["ratingKey"] == started))
            self.last_open["result"] = "started"
            if started != key:
                self.last_open["startedRatingKey"] = started
        return web.json_response({"requestId": request_id, "result": "pending"}, status=202)

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

    def snapshot(self) -> dict[str, Any]:
        """`self.status` as this build sends it: the Slice C fields only
        when the build lists them."""
        out = copy.deepcopy(self.status)
        if self.has("status.app"):
            out["app"]["foreground"] = self.foreground
            out["app"]["screen"] = "player" if out["player"] else "home"
        if self.has("subtitleOffset") and out["player"]:
            out["player"]["subtitles"]["offsetMs"] = self.subtitle_offset_ms
        if self.last_open is not None:
            out["lastOpen"] = copy.deepcopy(self.last_open)
        return out

    async def _status(self, request: web.Request) -> web.Response:
        if self.player:
            self.player["positionAt"] = now_iso()
        return web.json_response(self.snapshot())

    async def _artwork(self, request: web.Request) -> web.Response:
        kind = request.match_info["kind"]
        kinds = ("still", "art", "poster", "logo", "square") if self.art else ("still", "art", "poster")
        if kind not in kinds:
            return _error(404, "not_found", "Artwork kinds are still, art and poster.")
        if self.on_artwork:
            self.on_artwork()
        if not self.player:
            return _error(409, "nothing_playing", "Nothing is playing.")
        if kind not in self.player["artwork"]:
            return _error(404, "not_found", f"This item has no {kind} artwork.")
        # Like Cue: always the item playing NOW, whatever `key` says.
        rating_key = self.player["item"]["ratingKey"]
        if kind == "logo":
            return web.Response(body=png_with_alpha(rating_key), content_type="image/png")
        return web.Response(body=JPEG + rating_key.encode(), content_type="image/jpeg")

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
        if name in ("subtitles", "audio") and not ({"id", "language", "enabled"} & body.keys()):
            return _error(400, "invalid_parameter", f"{name} needs an id.")
        if name == "subtitle-offset":
            if ("offsetMs" in body) == ("deltaMs" in body):
                return _error(400, "invalid_parameter", "Send offsetMs or deltaMs.")
            value = body.get("offsetMs", body.get("deltaMs"))
            if not isinstance(value, int) or isinstance(value, bool) or abs(value) > 40000:
                return _error(400, "invalid_parameter", "Whole milliseconds.")
            if "offsetMs" in body and abs(value) > 20000:
                return _error(400, "invalid_parameter", "offsetMs must be within 20000.")
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
            return web.json_response(self.snapshot())
        elif name == "previous":
            prev = p["queue"].get("previous")
            if not prev:
                return _error(409, "no_previous", "There's no previous episode.")
            self.play(prev["ratingKey"])
            p = self.player
            p["item"].update(title=prev["title"], season=prev.get("season"), episode=prev.get("episode"))
            p["queue"] = {}
            p["positionMs"] = 0
        elif name == "subtitle-offset":
            if p["subtitles"]["selected"] is None:
                return _error(409, "unsupported", "Subtitles are off.")
            target = body.get("offsetMs", self.subtitle_offset_ms + body.get("deltaMs", 0))
            self.subtitle_offset_ms = max(-20000, min(20000, round(target / 50) * 50))
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
            block = p[name]
            if "language" in body:
                lang = str(body["language"]).lower()[:2]
                match = next((t for t in block["tracks"] if (t.get("language") or "")[:2] == lang), None)
                if match is None:
                    return _error(404, "unknown_track", f"No {body['language']} {name}.")
                wanted = match["id"]
            elif "enabled" in body:
                wanted = block["selected"] or block["tracks"][0]["id"] if body["enabled"] else None
            else:
                wanted = body["id"]
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
        return web.json_response(self.snapshot())

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
