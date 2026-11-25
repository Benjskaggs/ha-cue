"""Fixtures: a fake Cue server, a config entry, and a fake `apple_tv` entity."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import urlparse

import pytest

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    MockPlatform,
    mock_integration,
    mock_platform,
)

from custom_components.cue import media_player as cue_media_player
from custom_components.cue.const import CONF_APPLE_TV_ENTITY, DOMAIN

from .fake_cue import TOKEN, FakeCue

ATV_ENTITY = "media_player.living_room_apple_tv"
PLAYER = "media_player.living_room"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/ in every test."""


@pytest.fixture(autouse=True)
def fast_cold_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the cold-start waits so failures fail fast."""
    monkeypatch.setattr(cue_media_player, "WAKE_TIMEOUT", 1.0)
    monkeypatch.setattr(cue_media_player, "APP_TIMEOUT", 1.0)
    monkeypatch.setattr(cue_media_player, "SETTLE_DELAY", 0)
    monkeypatch.setattr(cue_media_player, "VERIFY_TIMEOUT", 1.0)
    monkeypatch.setattr(cue_media_player, "VERIFY_POLL", 0.05)


@pytest.fixture
async def fake_cue(socket_enabled: None) -> AsyncGenerator[FakeCue]:
    """A running fake Apple TV on 127.0.0.1."""
    fake = FakeCue()
    await fake.start()
    yield fake
    await fake.close()


def make_entry(
    fake: FakeCue,
    *,
    token: str = TOKEN,
    options: dict[str, Any] | None = None,
    unique_id: str | None = None,
) -> MockConfigEntry:
    """A config entry pointing at the fake. Keyed on the host by default, as
    Slice A made them (setup then adopts the fake's stable id)."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Living Room",
        unique_id=unique_id or fake.host,
        data={
            CONF_HOST: fake.host,
            CONF_PORT: fake.port,
            CONF_TOKEN: token,
            CONF_NAME: "Living Room",
        },
        options=options or {},
    )


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add and set up an entry."""
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
async def entry(hass: HomeAssistant, fake_cue: FakeCue) -> MockConfigEntry:
    """A set-up entry with no linked Apple TV."""
    config_entry = make_entry(fake_cue)
    await setup_entry(hass, config_entry)
    return config_entry


class FakeAppleTV(MediaPlayerEntity):
    """Stands in for HA's `apple_tv` media player.

    Like tvOS, a `cue://` link only reaches Cue when Cue is frontmost;
    otherwise tvOS shows "Open in Cue?" and nothing plays.
    """

    _attr_has_entity_name = False
    _attr_name = "Living Room Apple TV"
    _attr_unique_id = "fake-atv"
    _attr_supported_features = (
        MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.SELECT_SOURCE
        | MediaPlayerEntityFeature.PLAY_MEDIA
    )
    _attr_source_list = ["Cue", "YouTube"]

    def __init__(self, fake: FakeCue) -> None:
        self.entity_id = ATV_ENTITY
        self.fake = fake
        self.calls: list[tuple[Any, ...]] = []
        self.wakes = True
        self.launches = True
        self.link_plays = True
        self._attr_state = MediaPlayerState.ON
        self._attr_app_name: str | None = "YouTube"

    @property
    def app_name(self) -> str | None:
        return self._attr_app_name

    async def async_turn_on(self) -> None:
        self.calls.append(("turn_on",))
        if self.wakes:
            self._attr_state = MediaPlayerState.ON
            self.async_write_ha_state()

    async def async_select_source(self, source: str) -> None:
        self.calls.append(("select_source", source))
        if self.launches:
            self._attr_app_name = source
            self.async_write_ha_state()
            if source == "Cue" and self.fake._site is None:
                await self.fake.come_back()

    async def async_play_media(self, media_type: str, media_id: str, **kwargs: Any) -> None:
        self.calls.append(("play_media", str(media_type), media_id))
        if not (self.link_plays and self._attr_app_name == "Cue"):
            return  # tvOS's "Open in Cue?" prompt, or a link that didn't take
        url = urlparse(media_id)
        assert url.scheme == "cue" and url.netloc in ("play", "resume")
        key = url.path.lstrip("/")
        # `cue://resume/<show>` starts one of its episodes.
        self.fake.play("12345" if url.netloc == "resume" and key == "100" else key)


@pytest.fixture
async def apple_tv(hass: HomeAssistant, fake_cue: FakeCue) -> FakeAppleTV:
    """A fake apple_tv media player, set up through the media_player component."""
    atv = FakeAppleTV(fake_cue)

    async def _setup_platform(hass, config, async_add_entities, discovery_info=None):
        async_add_entities([atv])

    mock_integration(hass, MockModule("fake_atv"))
    mock_platform(hass, "fake_atv.media_player", MockPlatform(async_setup_platform=_setup_platform))
    assert await async_setup_component(
        hass, "media_player", {"media_player": {"platform": "fake_atv"}}
    )
    await hass.async_block_till_done()
    assert hass.states.get(ATV_ENTITY) is not None
    return atv


@pytest.fixture
async def linked_entry(
    hass: HomeAssistant, fake_cue: FakeCue, apple_tv: FakeAppleTV
) -> MockConfigEntry:
    """A set-up entry linked to the fake Apple TV."""
    config_entry = make_entry(fake_cue, options={CONF_APPLE_TV_ENTITY: ATV_ENTITY})
    await setup_entry(hass, config_entry)
    return config_entry
