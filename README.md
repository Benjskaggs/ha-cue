# Cue for Home Assistant

A [HACS](https://hacs.xyz) custom integration for **Cue**, the Plex player, on
Apple TV. It talks to Cue's local Control API (`/v1`, port 7788) and gives
Home Assistant:

| Entity | What it does |
|---|---|
| `media_player.<tv>` | State (`playing` / `paused` / `buffering` / `idle`, and `off` when Cue isn't open), title, show, season, episode, duration, position, artwork. Play, pause, seek, next episode (only when there is one). Play Media with a Plex rating key (needs a linked Apple TV entity, below). |
| `select.<tv>_subtitles` | Subtitle track, or Off |
| `select.<tv>_audio_track` | Audio track |
| `select.<tv>_speed` | Playback speed (this item only; Cue doesn't save it) |
| `button.<tv>_skip_intro`, `button.<tv>_skip_credits` | Available only while Cue says the marker can be skipped |
| `button.<tv>_restart` | Back to the start |
| `binary_sensor.<tv>_cue_open` | On while Cue answers on the LAN. The best automation trigger. |

It polls (`local_polling`, every 5 s by default). It doesn't control volume:
that belongs to your TV or soundbar.

> **Status: Slice B, for testing.** The Control API is a Developer Mode
> feature in Cue today, so this only works with a **Debug build** of Cue.
> Pairing, search and push updates come later.

## Requirements

- Home Assistant **2025.2** or newer.
- Cue on an Apple TV on the **same LAN** as Home Assistant. Cue refuses
  requests from public addresses. Builds before Slice B also refuse global
  IPv6 addresses (use the IPv4 address); newer ones accept global IPv6
  addresses on the Apple TV's own network.
- Optional, for Play Media and starting Cue when it's closed: Home Assistant's
  built-in [Apple TV integration](https://www.home-assistant.io/integrations/apple_tv/)
  set up for the same Apple TV.

## Set up Cue

1. In Cue on the Apple TV, turn on Developer Mode (Debug builds only).
2. Open **Settings › Connection › Home Assistant Control**, turn it on, and
   choose **Generate Token**. Note the token (`K7QM-2XRA-…`) and the Apple
   TV's address shown there.
3. Keep Cue open. tvOS suspends Cue shortly after it leaves the screen, and
   the API goes with it.

## Install

HACS › ⋮ › **Custom repositories** › add this repository's URL, category
**Integration** › install **Cue** › restart Home Assistant.

Or by hand: copy `custom_components/cue` into your config's
`custom_components/` and restart.

## Add the Apple TV

With Cue open on the Apple TV, Home Assistant **discovers it** (Bonjour
`_cue-control._tcp`) and shows it under **Settings › Devices & services ›
Discovered**. Choose **Add** and paste the token. Cue only advertises while
it's on screen (tvOS suspends it in the background), so open Cue first.

Or add it by hand: **Add integration › Cue**.

1. **Host**: the Apple TV's IP address. **Port**: leave 7788.
2. **Token**: paste the token from Cue. Case and dashes don't matter, and a
   leading `Bearer ` is stripped.

Cue builds with a stable device id (from Slice B) are tracked by that id, so
an IP change is picked up the next time Home Assistant sees the Apple TV
advertise. An entry added for an older build was keyed on its IP; it moves
to the id on its own once the Apple TV runs a newer Cue (entity ids, names
and history are kept). An older build also raises a **Cue is out of date**
notice in Settings › Repairs.

Then open the integration's **Configure** to set:

- **Poll interval**: 5–10 s.
- **Linked Apple TV media player**: the `media_player` entity Home
  Assistant's Apple TV integration made for this TV. Without it there's no
  Play Media.

### If the token changes

Generating a new token in Cue makes Home Assistant's requests fail with 401.
The integration then **stops polling at once** and asks you to
re-authenticate (Settings › Devices & services shows a "Re-authenticate"
card for Cue). It never retries a wrong
token: Cue locks an address out for 5 minutes after 10 wrong tokens in a
minute, and that would also lock out anything else on the same Home
Assistant host (rest_commands, scripts).

## What appears depends on the Cue build

Cue lists what it can do in `/v1/info.features`, and the integration only
creates what the build serves: Stop shows up once Cue serves `stop`,
Previous once it serves `previous`. Builds that don't list features get the
Slice A set.

## Play Media

```yaml
action: media_player.play_media
target:
  entity_id: media_player.living_room
data:
  media_content_type: episode
  media_content_id: "12345"          # a Plex ratingKey
  # or "0123abcd:12345" (server machineId:ratingKey), as the entity reports it
```

What happens, in this order:

1. If the Apple TV is asleep, the linked entity turns it on.
2. If Cue isn't the frontmost app, the linked entity selects the **Cue**
   source and waits until it reports `app_name: Cue`. (Sending a `cue://`
   link to an app that isn't on screen makes tvOS ask "Open in Cue?".)
3. The linked entity opens `cue://play/<ratingKey>` (with `?server=` when
   given).
4. The action only succeeds once **Cue itself** reports that item playing.
   If it doesn't within 25 s (for example the item is on a Plex server Cue
   isn't connected to, which only shows its page), the action fails with an
   error. It never reports success that didn't happen.

## Actions (services)

All target a Cue media player. Each answers with Cue's status in the shape
Cue's original (v0) API used: `ok`, `active`, `state`, `playing`, `title`,
`show`, `season`, `episode`, `positionMs`, `durationMs`, `rate`,
`subtitles` / `audio` (`selected`, `tracks[{id, title, language,
selected}]`), `markers.intro|credits` (`startMs`, `endMs`, `active`,
`skippable`), `hasNext`, `next`, plus `picture` (Home Assistant's image
proxy). The response is keyed by entity id.

| Action | Fields | Notes |
|---|---|---|
| `cue.get_status` | none | A fresh read from the Apple TV. Never fails while the entity is available: Cue closed or refusing answers `{ok: false, active: false, error}`. |
| `cue.select_subtitle` | one of `track_id`, `language` (`en`, `eng`, `English`), `off: true` | |
| `cue.select_audio` | one of `track_id`, `language` | |
| `cue.set_rate` | `rate` (above 0, up to 4) | This item only. |
| `cue.skip` | `marker`: `intro` or `credits` | |

Command actions fail with a clear error (nothing playing, no marker ahead,
no such track). Pass `return_errors: true` to get the error in the response
instead (`ok: false`, `error`, plus the fresh status), which is what a script
that must never stop wants. When the Cue entity is unavailable (wrong token,
Control turned off on the TV), entity actions don't run at all and Home
Assistant reports that no entity matched; scripts with `continue_on_error`
get past that too.

## Troubleshooting

| Message | Meaning |
|---|---|
| Couldn't reach the Apple TV | Cue isn't open, Home Assistant Control is off, or the address is wrong. |
| `not_lan` | Home Assistant reached Cue from an address Cue doesn't treat as local. |
| `disabled` | Home Assistant Control is off in Cue's Settings. |
| Too many wrong tokens | Wait the time shown; Cue locks the address for 5 minutes. |

Diagnostics (device page › Download diagnostics) include the last status
from Cue; the token is redacted.

## Development

```sh
python3.13 -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
pytest
```

Tests run Home Assistant 2026.2.3 (`pytest-homeassistant-custom-component`) against a
fake Cue server (`tests/fake_cue.py`) built from the v1 contract.
`tests/fixtures/openapi.yaml` is a snapshot of Cue's
`Docs/control-api/openapi.yaml`; `tests/test_spec.py` fails when the fake
drifts from it.
