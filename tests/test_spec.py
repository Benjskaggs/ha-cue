"""Drift guard: the fake (and so the integration's view of Cue) matches the
v1 contract snapshot in `fixtures/openapi.yaml`.

Refresh the snapshot from the Cue repo's `Docs/control-api/openapi.yaml`
whenever v1 grows; this test then says what the fake is missing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .fake_cue import IDLE, INFO, FakeCue, playing_status

SPEC = yaml.safe_load((Path(__file__).parent / "fixtures" / "openapi.yaml").read_text())
SCHEMAS = SPEC["components"]["schemas"]


def _resolve(schema: dict[str, Any]) -> dict[str, Any]:
    if "$ref" in schema:
        return _resolve(SCHEMAS[schema["$ref"].rsplit("/", 1)[1]])
    return schema


def _check(value: Any, schema: dict[str, Any], where: str) -> None:
    """Required keys, recursively (enough to catch shape drift)."""
    schema = _resolve(schema)
    if value is None:
        return
    for part in schema.get("allOf", []):
        _check(value, part, where)
    for option in schema.get("oneOf", []):
        if _resolve(option).get("type") != "null":
            _check(value, option, where)
    if isinstance(value, dict):
        for key in schema.get("required", []):
            assert key in value, f"{where}: missing {key}"
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                _check(value[key], sub, f"{where}.{key}")
                enum = _resolve(sub).get("enum")
                if enum and not isinstance(value[key], (dict, list)):
                    assert value[key] in enum, f"{where}.{key}: {value[key]!r} not in {enum}"
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            _check(item, schema["items"], f"{where}[{i}]")


def test_fake_routes_match_spec() -> None:
    """The fake serves exactly the spec's /v1 routes."""
    spec_paths = {
        (method.upper(), path)
        for path, ops in SPEC["paths"].items()
        if path.startswith("/v1/")
        for method in ops
    }
    fake_paths = {
        (route.method, route.resource.canonical)
        for route in FakeCue().app().router.routes()
        if route.method != "HEAD"
    }
    assert fake_paths == spec_paths


def test_fixtures_match_schemas() -> None:
    """The fake's status and info bodies have every required field."""
    _check(INFO, SCHEMAS["Info"], "info")
    _check(IDLE, SCHEMAS["Status"], "idle")
    _check(playing_status(), SCHEMAS["Status"], "playing")
    _check(playing_status()["player"], SCHEMAS["Player"], "player")


def test_error_codes_known() -> None:
    """Every code the integration branches on exists in the contract."""
    codes = set(SCHEMAS["ErrorCode"]["enum"])
    assert {"unauthorized", "not_lan", "disabled", "signed_out", "rate_limited",
            "nothing_playing", "no_next", "no_marker", "unknown_track"} <= codes
