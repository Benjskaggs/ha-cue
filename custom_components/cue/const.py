"""Constants for the Cue integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "cue"
MANUFACTURER: Final = "Cue"

DEFAULT_PORT: Final = 7788
DEFAULT_NAME: Final = "Apple TV"

# Entry data (beside host, port, token, name)
CONF_APP_VERSION: Final = "app_version"

# Options
CONF_APPLE_TV_ENTITY: Final = "apple_tv_entity"
DEFAULT_SCAN_INTERVAL: Final = 5
MIN_SCAN_INTERVAL: Final = 5
MAX_SCAN_INTERVAL: Final = 10

# Every request to the Apple TV gets this long, end to end (scout §5: one
# request per connection, 10 s server-side deadline; we give up sooner).
REQUEST_TIMEOUT: Final = 5
# Search, Continue Watching and open ask Plex first: give them most of Cue's
# own 10 s deadline.
LIBRARY_TIMEOUT: Final = 9

# How long to back off when Cue answers 429 without a usable Retry-After.
DEFAULT_RETRY_AFTER: Final = 300

# Speeds offered by the speed select. Cue accepts anything in (0, 4].
SPEEDS: Final = (0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0)

SUBTITLES_OFF: Final = "Off"

# Every v1 route a build before Slice B serves. Those builds don't send
# `/v1/info.features`, so this is what the integration assumes for them (and
# for any build it hasn't been able to ask yet).
LEGACY_FEATURES: Final = frozenset(
    {"info", "status", "artwork", "play", "pause", "toggle", "restart", "next",
     "seek", "rate", "subtitles", "audio", "skip"}
)

# Features (`/v1/info.features`) that decide which entities exist. Only a
# difference here, once a closed-at-setup Cue is first heard from, is worth
# a reload; the rest (artwork kinds, colours) are read live.
ENTITY_FEATURES: Final = frozenset({"subtitles", "audio", "rate", "skip", "restart", "subtitleOffset"})

# Slice C (Cue 1.2.1).
FEATURE_SUBTITLE_OFFSET: Final = "subtitleOffset"
# `status.app.foreground` / `.screen`.
FEATURE_APP_STATE: Final = "status.app"
# Cue clamps the subtitle delay to this many seconds either way.
SUBTITLE_OFFSET_LIMIT: Final = 20.0

# Slice D (Cue 1.2.1): voice "play X".
FEATURE_SEARCH: Final = "search"
FEATURE_OPEN: Final = "open"
FEATURE_CONTINUE: Final = "continueWatching"
# media_content_id values for the browse tree.
BROWSE_ROOT: Final = "cue_root"
BROWSE_CONTINUE: Final = "cue_continue"

# The couch-card art (contract `cue-art-contract.md` §2): extra media player
# attributes, each an HA-proxied URL, and the feature a build must list for
# it (None: every v1 build serves the kind).
ART_KINDS: Final = (
    ("art", "entity_picture_art", None),
    ("square", "entity_picture_square", "artwork.square"),
    ("logo", "entity_picture_logo", "artwork.logo"),
)
ATTR_ACCENT_COLOR: Final = "accent_color"
FEATURE_COLORS: Final = "colors"
# Path segment for the art in HA's media_player image proxy
# (`/api/media_player_proxy/<entity>/browse_media/cue_<kind>/<ratingKey>`).
ART_CONTENT_PREFIX: Final = "cue_"
# Images kept per entry, and the biggest one kept (a clearLogo can be huge;
# bigger ones are still served, just not cached).
ART_CACHE_SIZE: Final = 12
ART_CACHE_MAX_BYTES: Final = 8 * 1024 * 1024

# The repair issue raised for a pre-Slice-B build (no `features`, no `id`).
ISSUE_OUTDATED: Final = "outdated_cue"

# The app name HA's `apple_tv` integration reports, and the source to select.
APPLE_TV_APP_NAME: Final = "Cue"
