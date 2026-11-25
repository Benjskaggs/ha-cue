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

# How long to back off when Cue answers 429 without a usable Retry-After.
DEFAULT_RETRY_AFTER: Final = 300

# Speeds offered by the speed select. Cue accepts anything in (0, 4].
SPEEDS: Final = (0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0)

SUBTITLES_OFF: Final = "Off"

# The app name HA's `apple_tv` integration reports, and the source to select.
APPLE_TV_APP_NAME: Final = "Cue"
