"""Slice E (Cue 1.2.1): push over `/v1/events`, the playback event entity,
`cue_event` on the bus and device triggers. Polling works out the same
events when there's no stream."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.components import automation
from homeassistant.components.device_automation import DeviceAutomationType
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
    async_get_device_automations,
)

from custom_components.cue.const import DOMAIN, EVENT_CUE, EVENT_TYPES

from .conftest import PLAYER, make_entry, setup_entry
from .fake_cue import DEVICE_ID, FakeCue

EVENT_ENTITY = "event.living_room_playback"
OPEN = "binary_sensor.living_room_cue_open"


async def _until(predicate, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def _streaming(hass: HomeAssistant, fake: FakeCue) -> MockConfigEntry:
    fake.enable_events()
    entry = make_entry(fake, unique_id=DEVICE_ID)
    await setup_entry(hass, entry)
    await _until(lambda: entry.runtime_data.streaming)
    return entry


@pytest.fixture
async def events(hass: HomeAssistant) -> list[Event]:
    return async_capture_events(hass, EVENT_CUE)


def _device(hass: HomeAssistant) -> dr.DeviceEntry:
    return next(d for d in dr.async_get(hass).devices.values() if (DOMAIN, DEVICE_ID) in d.identifiers)


def _types(events: list[Event]) -> list[str]:
    return [e.data["type"] for e in events]


async def test_stream_drives_state(hass: HomeAssistant, fake_cue: FakeCue, events: list[Event]) -> None:
    fake_cue.play()
    entry = await _streaming(hass, fake_cue)
    coordinator = entry.runtime_data
    assert coordinator.update_interval == timedelta(seconds=60)
    polls = fake_cue.count("/v1/status")

    # A pause pushed by Cue: the entity follows with no poll.
    fake_cue.player["state"] = "paused"
    fake_cue.push_status()
    await _until(lambda: hass.states.get(PLAYER).state == "paused")
    assert fake_cue.count("/v1/status") == polls
    await _until(lambda: _types(events) == ["paused"])
    device = _device(hass)
    assert events[0].data["device_id"] == device.id
    assert events[0].data["series"] == "Grey's Anatomy" and events[0].data["rating_key"] == "12345"
    state = hass.states.get(EVENT_ENTITY)
    assert state.attributes["event_type"] == "paused"
    assert state.attributes["title"] == "Before and After"

    # A position report moves the position only.
    fake_cue.push("position", {"positionMs": 600000, "positionAt": "2026-11-25T20:00:00.000Z", "rate": 1.0, "state": "paused"})
    await _until(lambda: hass.states.get(PLAYER).attributes["media_position"] == 600.0)

    # Entering the credits: the status shows the marker active.
    fake_cue.player["state"] = "playing"
    fake_cue.set_marker("credits", skippable=True)
    fake_cue.push_status()
    fake_cue.push("marker", {"type": "credits", "phase": "started", "ratingKey": "12345"})
    await _until(lambda: _types(events) == ["paused", "resumed", "credits_started"])

    # The next episode.
    fake_cue.play("12346")
    fake_cue.push_status()
    await _until(lambda: _types(events)[-1] == "item_changed")

    # Unloading the entry closes the stream (the task is the entry's).
    await hass.config_entries.async_unload(entry.entry_id)
    await _until(lambda: coordinator._stream_task is None)  # noqa: SLF001
    assert not coordinator.streaming


async def test_foreground_lost_then_polling(hass: HomeAssistant, fake_cue: FakeCue, events: list[Event]) -> None:
    """Cue says it's leaving, closes the stream, and stops answering."""
    fake_cue.play()
    entry = await _streaming(hass, fake_cue)
    fake_cue.push("app", {"foreground": False})
    await _until(lambda: hass.states.get(OPEN).state == STATE_OFF)
    await fake_cue.go_away()
    await _until(lambda: not entry.runtime_data.streaming)
    await _until(lambda: hass.states.get(PLAYER).state == "off")
    assert _types(events) == ["foreground_lost"]  # once, not again when it stops answering
    assert entry.runtime_data.update_interval == timedelta(seconds=5)
    await hass.config_entries.async_unload(entry.entry_id)


async def test_stream_comes_back(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    """Back in the foreground: the next poll that reaches Cue reopens it."""
    fake_cue.play()
    entry = await _streaming(hass, fake_cue)
    await fake_cue.go_away()
    await _until(lambda: not entry.runtime_data.streaming)
    await fake_cue.come_back()
    entry.runtime_data._stream_retry_at = 0  # noqa: SLF001 (skip the wait)
    await entry.runtime_data.async_refresh()
    await _until(lambda: entry.runtime_data.streaming)
    await hass.config_entries.async_unload(entry.entry_id)


async def test_busy_falls_back_to_polling(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    fake_cue.enable_events()
    fake_cue.refuse_streams = (503, "busy")
    fake_cue.play()
    entry = make_entry(fake_cue, unique_id=DEVICE_ID)
    await setup_entry(hass, entry)
    await _until(lambda: entry.runtime_data._stream_task is None and fake_cue.count("/v1/events") == 1)  # noqa: SLF001
    assert not entry.runtime_data.streaming
    assert entry.runtime_data.update_interval == timedelta(seconds=5)
    # It doesn't retry on every poll.
    await entry.runtime_data.async_refresh()
    await asyncio.sleep(0.05)
    assert fake_cue.count("/v1/events") == 1
    assert hass.states.get(PLAYER).state == "playing"


async def test_wrong_token_on_the_stream_starts_reauth(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    fake_cue.enable_events()
    fake_cue.refuse_streams = (401, "unauthorized")
    entry = make_entry(fake_cue, unique_id=DEVICE_ID)
    await setup_entry(hass, entry)
    await _until(lambda: entry.runtime_data.auth_failed)
    await hass.async_block_till_done()
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())


async def test_polling_works_out_the_same_events(
    freezer: FrozenDateTimeFactory, hass: HomeAssistant, fake_cue: FakeCue, events: list[Event]
) -> None:
    """A build without `/v1/events`: the poll derives them."""
    await setup_entry(hass, make_entry(fake_cue))

    async def poll() -> None:
        freezer.tick(timedelta(seconds=6))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)

    fake_cue.play()
    await poll()
    fake_cue.set_marker("intro", skippable=True)
    await poll()
    fake_cue.player["state"] = "paused"
    await poll()
    fake_cue.player["state"] = "ended"
    await poll()
    fake_cue.idle()
    await poll()
    await fake_cue.go_away()
    await poll()
    assert _types(events) == ["started", "intro_started", "paused", "ended", "stopped", "foreground_lost"]


async def test_device_triggers(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    fake_cue.play()
    entry = await _streaming(hass, fake_cue)
    device = _device(hass)
    triggers = await async_get_device_automations(hass, DeviceAutomationType.TRIGGER, device.id)
    assert {t["type"] for t in triggers if t["domain"] == DOMAIN} == set(EVENT_TYPES)

    calls: list[str] = []
    hass.services.async_register("test", "automation", lambda call: calls.append(call.data["type"]))
    assert await async_setup_component(hass, automation.DOMAIN, {
        automation.DOMAIN: {
            "trigger": {"platform": "device", "domain": DOMAIN, "device_id": device.id, "type": "credits_started"},
            "action": {"service": "test.automation", "data": {"type": "{{ trigger.event.data.type }}"}},
        }
    })
    fake_cue.set_marker("credits", skippable=True)
    fake_cue.push_status()
    await _until(lambda: calls == ["credits_started"])
    await hass.config_entries.async_unload(entry.entry_id)
