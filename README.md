# Cue for Home Assistant

A [HACS](https://hacs.xyz) integration for **Cue**, the Plex player for
Apple TV. Home Assistant sees what's playing and controls it: play, pause,
seek, episodes, subtitles, audio, speed, skip intro and credits, "play X"
by voice, and events for your automations.

## Requirements

- **Cue 1.2.1 or later** on the Apple TV, with **Settings › Connection ›
  Home Assistant Control** turned on. It's off by default.
- Home Assistant 2025.2 or later, on the same home network as the Apple TV.
- Optional: HA's own **Apple TV** integration for the same TV, so Home
  Assistant can wake the TV and start Cue when it isn't open.

## Install

1. In HACS: **⋮ › Custom repositories**, add
   `https://github.com/Benjskaggs/ha-cue` as an **Integration**.
2. Install **Cue**, then restart Home Assistant.

## Set up

1. Open Cue on the Apple TV. Home Assistant finds it on the network and
   offers it under **Settings › Devices & services** (it can only find Cue
   while Cue is open). Or add it yourself: **Add integration › Cue**, with
   the Apple TV's address (Cue's Settings › Connection shows it).
2. Choose **Pair with a code on the TV**. The TV shows a six-digit code;
   type it into Home Assistant.

   No pairing (a script, or Cue older than 1.2.1)? Choose **Use a token**
   instead. On the TV: **Make a Token for Scripts**, then paste it in.
3. Optional, in the integration's **Configure**: link the HA Apple TV
   entity for the same TV, so Play Media can start Cue when it's closed.

Cue's Settings lists every paired controller with when it was last seen,
and **Revoke**. Removing the integration revokes its own pairing.

## What you get

| Entity | What it does |
|---|---|
| `media_player.<tv>` | `playing`, `paused`, `idle`, or `off` when Cue isn't open. Title, show, season, episode, position, artwork. Play, pause, seek, stop (closes the player, like Menu), next and previous episode, Play Media, Search Media, and the media browser (Continue Watching). Attributes `accent_color`, `entity_picture_art`, `entity_picture_square`, `entity_picture_logo` for a Now Playing card. |
| `select.<tv>_subtitles` / `_audio_track` / `_speed` | Pick a track (or subtitles Off), or a speed for this item. |
| `number.<tv>_subtitle_timing` | Subtitle delay in seconds, for this stream only. |
| `button.<tv>_skip_intro` / `_skip_credits` / `_restart` | Skip buttons are available only while there's something to skip. |
| `binary_sensor.<tv>_cue_open` | On while Cue is open on the TV. Attribute `screen`: `player`, `home`, `search`, … |
| `event.<tv>_playback` | `started`, `paused`, `resumed`, `stopped`, `ended`, `item_changed`, `intro_started`, `credits_started`, `foreground_lost` (Cue left the screen). Also device triggers, and `cue_event` on the bus. |

Cue pushes changes as they happen (within half a second), with a slow poll
as a safety net. Volume isn't here: that belongs to your TV or soundbar.

### Actions

All target the Cue media player and answer with data, for scripts and
voice assistants.

| Action | What it answers |
|---|---|
| `cue.get_status` | What's playing, in detail (tracks, markers, next episode). Never fails: when Cue is closed it says so. |
| `cue.select_subtitle`, `cue.select_audio` | By `track_id` or `language` (`en`, `eng`, `English`); subtitles also `off: true`. |
| `cue.set_rate`, `cue.skip` | Speed (above 0, up to 4); skip `intro` or `credits`. |
| `cue.search` | `query`, optional `type` and `limit`. Titles in your Plex library; never plays. |
| `cue.continue_watching` | Cue Home's Continue Watching row. |
| `cue.cast` | The cast of what's playing: name, character, and a picture. |
| `cue.on_screen` | Who's on screen in the paused picture (see Privacy). |
| `cue.filmography` | `person`: what else they're in, in your library. |

Each title in a result has a `media_content_id` and `media_content_type` to
pass to `media_player.play_media`. Command actions take
`return_errors: true` to answer an error instead of failing.

## Automations

Dim the lights when a film starts:

```yaml
triggers:
  - trigger: state
    entity_id: event.living_room_playback
    attribute: event_type
    to: started
actions:
  - action: light.turn_on
    target: { entity_id: light.living_room }
    data: { brightness_pct: 10 }
```

Bring them up when the credits roll:

```yaml
triggers:
  - trigger: state
    entity_id: event.living_room_playback
    attribute: event_type
    to: credits_started
actions:
  - action: light.turn_on
    target: { entity_id: light.living_room }
    data: { brightness_pct: 70, transition: 10 }
```

Pause when the doorbell rings:

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.front_door_doorbell
    to: "on"
conditions:
  - condition: state
    entity_id: media_player.living_room
    state: playing
actions:
  - action: media_player.media_pause
    target: { entity_id: media_player.living_room }
```

## Voice

Expose the Cue media player to Assist, then say "play *The Matrix* on the
living room TV". Assist searches Cue's library and plays the best match. A
show carries on where you are in it. Results come from the Plex server Cue
is connected to.

## Privacy

- **Your network only.** Cue answers only devices on its local network, over
  plain HTTP on port 7788. Don't forward that port.
- **Tokens are stored hashed** in the Apple TV's Keychain, on that TV only.
  Revoke any controller in Cue's Settings.
- **Your Plex token never leaves the TV.** Artwork and headshots are fetched
  by Cue and passed on as images.
- **Who's on screen** comes from Cue's Onstage, and only when you've turned
  on **Share Who's on Screen** in Cue's Settings › Connection (off by
  default), Onstage is on (set on the TV, never from Home Assistant), and
  playback is paused. It shares names and characters only, never pictures
  of faces. Those names can go on to a voice assistant from Home Assistant.
  The cast list (`cue.cast`) is Plex's credits and needs no switch.

## Troubleshooting

| Problem | What to do |
|---|---|
| The TV shows as off, or isn't discovered | **Cue must be open.** tvOS suspends Cue soon after it leaves the screen, and it can't answer then. Link the HA Apple TV entity so Play Media can start it. |
| Discovery finds nothing (Docker) | Run Home Assistant with host networking, so it sees Bonjour (`_cue-control._tcp`). Or add the TV by address. |
| "Home Assistant Control is turned off" | Turn it on in Cue › Settings › Connection. |
| Cue asks to re-authenticate | The pairing was revoked or the token replaced. Pair again from the notification. |
| "Too many wrong tokens" | Cue locks an address out for 5 minutes after repeated wrong tokens. Wait, then re-authenticate once. Clients already working from that address keep working. |
| Some entities or actions are missing | The Cue build is older. Update Cue; Home Assistant shows only what the build supports, and raises a repair when it's too old. |

Diagnostics (device page › Download diagnostics) include Cue's last status,
with the token removed.

## Development

```sh
python3.13 -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
pytest
```

The tests run Home Assistant against a fake Cue (`tests/fake_cue.py`) built
from Cue's API contract; `tests/fixtures/openapi.yaml` is a snapshot of
Cue's `Docs/control-api/openapi.yaml`, and `tests/test_spec.py` fails when
the fake drifts from it.
