"""Couch-card art (cue-art-contract.md §2): `accent_color` and the
`entity_picture_{art,square,logo}` attributes, served by HA's own
media_player image proxy with the entity token, cached per (kind, ratingKey)."""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.cue import coordinator as cue_coordinator

from .conftest import PLAYER, make_entry, setup_entry
from .fake_cue import ACCENT, JPEG, FakeCue, png_with_alpha

ART_ATTRS = ("entity_picture_art", "entity_picture_square", "entity_picture_logo")


def _attrs(hass: HomeAssistant) -> dict:
    return hass.states.get(PLAYER).attributes


async def _start(hass: HomeAssistant, fake: FakeCue, rating_key: str = "12345") -> None:
    fake.play(rating_key)
    await setup_entry(hass, make_entry(fake))


def _art_gets(fake: FakeCue, kind: str) -> int:
    return fake.count(f"/v1/artwork/{kind}")


async def test_attributes(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    """Exact names and URL shape; entity_picture keeps its meaning."""
    await _start(hass, fake_cue)
    attrs = _attrs(hass)
    assert attrs["accent_color"] == ACCENT
    for kind in ("art", "square", "logo"):
        url = attrs[f"entity_picture_{kind}"]
        assert url.startswith(f"/api/media_player_proxy/{PLAYER}/browse_media/cue_{kind}/12345?token=")
    assert attrs["entity_picture"].startswith(f"/api/media_player_proxy/{PLAYER}?token=")


async def test_logo_bytes_intact_and_cached(
    hass: HomeAssistant, fake_cue: FakeCue, hass_client: ClientSessionGenerator
) -> None:
    """The PNG comes through byte for byte (alpha kept), fetched once."""
    await _start(hass, fake_cue)
    client = await hass_client()
    url = _attrs(hass)["entity_picture_logo"]
    for _ in range(3):
        resp = await client.get(url)
        assert resp.status == HTTPStatus.OK
        assert resp.content_type == "image/png"
        body = await resp.read()
        assert body == png_with_alpha("12345")
        assert body[25] == 6  # IHDR colour type 6: RGBA
    assert _art_gets(fake_cue, "logo") == 1

    resp = await client.get(_attrs(hass)["entity_picture_square"])
    assert resp.content_type == "image/jpeg" and await resp.read() == JPEG + b"12345"
    resp = await client.get(_attrs(hass)["entity_picture_art"])
    assert resp.status == HTTPStatus.OK
    assert _art_gets(fake_cue, "square") == 1 and _art_gets(fake_cue, "art") == 1


async def test_polls_never_fetch_art(
    freezer: FrozenDateTimeFactory, hass: HomeAssistant, fake_cue: FakeCue
) -> None:
    """Attributes change only with the item, and polling fetches no images."""
    await _start(hass, fake_cue)
    before = dict(_attrs(hass))
    for _ in range(5):
        freezer.tick(timedelta(seconds=6))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert fake_cue.count("/v1/status") >= 6
    assert not [p for p in fake_cue.paths() if p.startswith("/v1/artwork/")]
    after = _attrs(hass)
    assert {k: after[k] for k in ART_ATTRS} == {k: before[k] for k in ART_ATTRS}


async def test_item_change(
    hass: HomeAssistant, fake_cue: FakeCue, hass_client: ClientSessionGenerator
) -> None:
    """A new item gets new URLs; the old URL never fetches the new item's art."""
    await _start(hass, fake_cue)
    client = await hass_client()
    old = _attrs(hass)["entity_picture_logo"]
    assert (await client.get(old)).status == HTTPStatus.OK

    await hass.services.async_call("media_player", "media_next_track", {"entity_id": PLAYER}, blocking=True)
    new = _attrs(hass)["entity_picture_logo"]
    assert "/cue_logo/12346?" in new and new != old

    # The old item's logo is still in the cache; it's never refetched.
    resp = await client.get(old)
    assert resp.status == HTTPStatus.OK and await resp.read() == png_with_alpha("12345")
    resp = await client.get(new)
    assert await resp.read() == png_with_alpha("12346")
    assert _art_gets(fake_cue, "logo") == 2

    # An item that isn't playing and was never cached: no request to Cue.
    stale = old.replace("/12345?", "/99999?")
    assert (await client.get(stale)).status != HTTPStatus.OK
    assert _art_gets(fake_cue, "logo") == 2


async def test_item_changes_mid_fetch(
    hass: HomeAssistant, fake_cue: FakeCue, hass_client: ClientSessionGenerator
) -> None:
    """Cue answers for whatever plays NOW: if the item changed while we
    fetched, the bytes belong to another item and are neither served nor kept."""
    await _start(hass, fake_cue)
    coordinator = hass.config_entries.async_entries("cue")[0].runtime_data
    client = await hass_client()
    url = _attrs(hass)["entity_picture_logo"]

    def switch() -> None:
        fake_cue.play("777")
        coordinator.data.status["player"]["item"]["ratingKey"] = "777"

    fake_cue.on_artwork = switch
    assert (await client.get(url)).status != HTTPStatus.OK
    assert ("logo", "12345") not in coordinator._art_cache  # noqa: SLF001
    assert ("logo", "777") not in coordinator._art_cache  # noqa: SLF001


async def test_needs_auth_or_token(
    hass: HomeAssistant, fake_cue: FakeCue, hass_client_no_auth: ClientSessionGenerator
) -> None:
    """Same rule as entity_picture: HA auth, or the entity's signed token."""
    await _start(hass, fake_cue)
    client = await hass_client_no_auth()
    url = _attrs(hass)["entity_picture_logo"]
    assert (await client.get(url)).status == HTTPStatus.OK
    assert (await client.get(url.split("?")[0])).status == HTTPStatus.UNAUTHORIZED
    assert (await client.get(url.split("?")[0] + "?token=wrong")).status == HTTPStatus.UNAUTHORIZED


async def test_slice_b_build_unchanged(
    hass: HomeAssistant, fake_cue: FakeCue, hass_client: ClientSessionGenerator
) -> None:
    """A build without the art features: no new attributes, art proxy only
    for `art` (which every build serves), entity_picture as before."""
    fake_cue.without_art()
    await _start(hass, fake_cue)
    attrs = _attrs(hass)
    assert "accent_color" not in attrs
    assert "entity_picture_logo" not in attrs and "entity_picture_square" not in attrs
    assert "entity_picture_art" in attrs
    assert attrs["entity_picture"].startswith(f"/api/media_player_proxy/{PLAYER}?")

    client = await hass_client()
    forged = attrs["entity_picture_art"].replace("/cue_art/", "/cue_logo/")
    assert (await client.get(forged)).status != HTTPStatus.OK
    assert _art_gets(fake_cue, "logo") == 0


async def test_legacy_build_unchanged(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    """A pre-Slice-B build: same as before, nothing new appears."""
    fake_cue.legacy()
    await _start(hass, fake_cue)
    attrs = _attrs(hass)
    assert "accent_color" not in attrs and "entity_picture_logo" not in attrs


async def test_feature_without_art(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    """The build lists the features, but this title has no logo or square."""
    fake_cue.play()
    del fake_cue.player["artwork"]["logo"]
    del fake_cue.player["artwork"]["square"]
    del fake_cue.player["colors"]
    await setup_entry(hass, make_entry(fake_cue))
    attrs = _attrs(hass)
    assert "entity_picture_logo" not in attrs and "entity_picture_square" not in attrs
    assert "accent_color" not in attrs and "entity_picture_art" in attrs


async def test_art_listed_but_feature_missing(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    """Status mentions a logo, but the build doesn't advertise the feature."""
    fake_cue.info["features"] = [f for f in fake_cue.info["features"] if f not in ("artwork.logo", "colors")]
    await _start(hass, fake_cue)
    attrs = _attrs(hass)
    assert "entity_picture_logo" not in attrs and "accent_color" not in attrs
    assert "entity_picture_square" in attrs


@pytest.mark.parametrize("accent", ["red", "#E0A0", "E0A030", "#GGGGGG", 12])
async def test_bad_accent_omitted(hass: HomeAssistant, fake_cue: FakeCue, accent) -> None:
    fake_cue.play()
    fake_cue.player["colors"]["accent"] = accent
    await setup_entry(hass, make_entry(fake_cue))
    assert "accent_color" not in _attrs(hass)


async def test_lowercase_accent_normalised(hass: HomeAssistant, fake_cue: FakeCue) -> None:
    fake_cue.play()
    fake_cue.player["colors"]["accent"] = "#e0a030"
    await setup_entry(hass, make_entry(fake_cue))
    assert _attrs(hass)["accent_color"] == "#E0A030"


async def test_big_images_served_not_cached(
    hass: HomeAssistant,
    fake_cue: FakeCue,
    hass_client: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cue_coordinator, "ART_CACHE_MAX_BYTES", 10)
    await _start(hass, fake_cue)
    client = await hass_client()
    url = _attrs(hass)["entity_picture_logo"]
    for _ in range(2):
        assert await (await client.get(url)).read() == png_with_alpha("12345")
    assert _art_gets(fake_cue, "logo") == 2


async def test_cache_is_bounded(
    hass: HomeAssistant, fake_cue: FakeCue, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cue_coordinator, "ART_CACHE_SIZE", 2)
    await _start(hass, fake_cue)
    coordinator = hass.config_entries.async_entries("cue")[0].runtime_data
    for kind in ("art", "square", "logo"):
        assert await coordinator.async_get_item_artwork(kind, "12345") is not None
    assert list(coordinator._art_cache) == [("square", "12345"), ("logo", "12345")]  # noqa: SLF001


async def test_late_info_needs_no_reload_for_art(
    freezer: FrozenDateTimeFactory, hass: HomeAssistant, fake_cue: FakeCue
) -> None:
    """Cue closed at setup: once it answers, the art attributes appear on
    the next poll without reloading the entry (art doesn't shape entities)."""
    from .fake_cue import DEVICE_ID

    # A 1.2 build: art, but nothing new that shapes entities (a 1.2.1
    # build's `subtitleOffset` does reload: test_late_entity_feature_reloads).
    fake_cue.before_slice_c()
    await fake_cue.go_away()
    entry = make_entry(fake_cue, unique_id=DEVICE_ID)
    await setup_entry(hass, entry)
    coordinator = entry.runtime_data
    await fake_cue.come_back()
    fake_cue.play()
    for _ in range(2):
        freezer.tick(timedelta(seconds=6))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data is coordinator  # not reloaded
    assert coordinator.info is not None
    assert "entity_picture_logo" in _attrs(hass)


async def test_late_entity_feature_reloads(
    freezer: FrozenDateTimeFactory, hass: HomeAssistant, fake_cue: FakeCue
) -> None:
    """Cue closed at setup, then a 1.2.1 build answers: `subtitleOffset`
    adds an entity, so the entry reloads once and the number appears."""
    from .fake_cue import DEVICE_ID

    await fake_cue.go_away()
    entry = make_entry(fake_cue, unique_id=DEVICE_ID)
    await setup_entry(hass, entry)
    assert hass.states.get("number.living_room_subtitle_timing") is None
    await fake_cue.come_back()
    fake_cue.play()
    for _ in range(3):
        freezer.tick(timedelta(seconds=6))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("number.living_room_subtitle_timing") is not None
