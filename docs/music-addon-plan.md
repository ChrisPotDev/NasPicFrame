# Music add-on: plan

**Status:** phases 1–3 are built on the `music-addon` branch and pass CI. Nothing has been tested on the Pi yet: the prototype checks in [§10](#10-prototype-checks-before-phase-1) are now the hardware acceptance tests. The branch merges after the frame's `v2.0.0` release and those checks. Usage is documented in [addons/music/README.md](../addons/music/README.md).

**Changes from the original plan:**
- **No `update-music.sh`.** Upgrading is `git pull`, then re-running `install-music.sh` (it migrates `music.ini`), the same as the frame. `update-ytdlp.sh` updates yt-dlp on its own, nightly or by hand.
- **P1 and P2 are confirmed by the library's documentation:** multiple senders and "Link with TV code" are both supported by `yt-cast-receiver` 2.1. They still need checking with the YouTube Music app on the Pi.
- **yt-dlp comes from PyPI** (`yt-dlp[default]`, in a private virtual environment) rather than a downloaded binary, so that its YouTube JavaScript component is included. It runs with `--js-runtimes node`.
- **Sources are detected from PipeWire.** The coordinator detects which source is playing from the audio streams themselves, using fixed application names (`picframe-youtube/-spotify/-airplay`), rather than from per-receiver hooks.

## 1. Goal

Let guests, or the owner, play music through the room's amplifier from their phones when entertaining. There's nothing to install and no account on the Pi; the Pi just appears in the app's cast button.

| | Android | iPhone |
|---|---|---|
| **YouTube Music** (primary) | YouTube cast receiver | YouTube cast receiver, or AirPlay |
| **Spotify** (Premium guests) | Spotify Connect | Spotify Connect |
| **Anything else** | n/a (out of scope) | AirPlay |

## 2. Relationship to the core frame

- A **separate, optional install** in `addons/music/`. It isn't installed by the core `install.sh`.
- **Independent in both directions.** The frame runs without the add-on, and the add-on runs on a Pi with no frame. Installing, updating or removing either one never touches the other.
- **No core changes in phases 1 and 2.** Phase 3 adds one small core feature that is **off unless the add-on is present**: the frame shows a now-playing card from a file the add-on writes ([§7](#7-frame-integration-phase-3)).
- Same conventions as the core: an INI config that is migrated on upgrade (never overwritten), systemd services with automatic restart, logs in the journal, CI on every push.

## 3. Target platform

- **Raspberry Pi OS Trixie (Debian 13), 64-bit, desktop (labwc)**: the primary target. Bookworm is best-effort.
- **Raspberry Pi 3 Model B** as the baseline; a Pi 4 or 5 just adds headroom.
- **Audio outputs:** a USB DAC (the owner's setup) and the 3.5 mm headphone jack, with HDMI as an option. Each goes to the external amplifier.
- **Audio stack:** PipeWire with WirePlumber, the Trixie default. All add-on services run as **systemd user services** under the frame user, with lingering enabled, so they share the user's PipeWire session and get mixed instead of fighting over the device.

## 4. Components

### 4.1 YouTube cast receiver (primary)
- **Function:** the Pi appears in the cast menu of the **YouTube Music** and **YouTube** apps, on Android and iPhone.
- **Protocol:** DIAL for discovery on the local network, and the YouTube **Lounge API** for the session and commands (play, pause, seek, skip, volume, queue).
- **Build:** a small Node.js service using `yt-cast-receiver`. It hands each video ID to **mpv**, which fetches the audio with **yt-dlp** and plays it through PipeWire, audio only.
- **Multiple guests:** several phones connected at once, sharing one queue.
- **TV-code pairing** (if supported, [§10](#10-prototype-checks-before-phase-1)): guests on another network can link by code; the frame displays the code.
- **Caveats:** this is an unofficial, reverse-engineered protocol, and playing YouTube streams outside YouTube's own apps is against YouTube's terms of service. It **will break occasionally** when YouTube changes things; updates are covered in [§8](#8-updates). Content is fetched anonymously, so private or uploaded tracks may not play.

### 4.2 Spotify Connect
- **librespot**, from the `raspotify` package, run as a user service; raspotify's own system service is disabled.
- Zeroconf discovery: any **Spotify Premium** user on the same network can select the Pi, with no credentials stored on it.
- Volume normalisation on, starting at the configured `start_volume`.

### 4.3 AirPlay
- **shairport-sync** as a user service with the PipeWire backend. AirPlay 2 if the Debian build supports it, otherwise AirPlay 1 ([§10](#10-prototype-checks-before-phase-1)). AirPlay 1 is fine for a single room.
- A fallback for iPhone guests that works with any app, including when the YouTube receiver is broken.
- An optional password (`airplay_password`), empty by default.

### 4.4 Coordinator
A small Python service that is the single source of truth for "who is playing":
- **Last source wins:** when a source starts, the others are stopped:
  - **YouTube:** paused through the receiver's own control interface.
  - **AirPlay:** stopped over shairport-sync's D-Bus remote-control interface.
  - **Spotify:** librespot has no remote pause, so its session is ended by restarting the service. The guest's app shows "disconnected".
- **Lock:** when the lock is on (from Home Assistant), new sources are refused and the current one keeps playing.
- **Volume policy** ([§6](#6-behaviour)): applies the max-volume cap, the quiet-hours cap and the per-session start volume.
- **Output selection** ([§5](#5-configuration)): prefers the USB DAC, falls back to the jack, and switches back when the DAC returns.
- **Idle:** clears the YouTube queue after `idle_clear_minutes` without playback.
- **Now playing:** writes the now-playing file for the frame ([§7](#7-frame-integration-phase-3)) and publishes to MQTT ([§7.2](#72-home-assistant)).

## 5. Configuration

`/etc/picframe/music.ini`. Upgrades migrate it with the same text merge as the core config: new options are added, existing values and comments are never changed, and a backup is kept. Changes are picked up within about 5 seconds, and the affected receivers restart.

```ini
[music]
; Name guests see in YouTube Music's cast menu, Spotify's device list and AirPlay.
name = Living Room
; Optional per-app names; empty = use `name`.
youtube_name =
spotify_name =
airplay_name =

; Which receivers run.
youtube = yes
spotify = yes
airplay = yes
; AirPlay password; empty = open to anyone on the network.
airplay_password =

[output]
; auto       = USB DAC if present, otherwise the headphone jack; follows plug/unplug
; usb        = USB DAC only (silent if missing)
; headphones = 3.5 mm jack only
; hdmi       = the screen's speakers
output = auto
; Pin a specific USB device by (part of) its name, if more than one is connected.
usb_device =

[volume]
; Percent. Guests' phone volume controls the range from 0 up to the cap.
max_volume = 80
; Level each new session starts at, so the first song is never a blast.
start_volume = 40
; Lower cap during quiet hours (HH:MM-HH:MM, may cross midnight; empty = none).
quiet_hours = 22:00-07:00
quiet_max_volume = 50

[behaviour]
; New guests may take over what's playing (unless locked from Home Assistant).
allow_takeover = yes
; Clear the YouTube queue after this many minutes without playback (0 = never).
idle_clear_minutes = 30

[updates]
; Update yt-dlp nightly from its official releases, keeping the previous version.
auto_update_ytdlp = yes

[mqtt]
; Home Assistant entities (phase 3). The installer pre-fills these from the
; frame's config if the frame is installed.
enabled = no
host = homeassistant.local
port = 1883
username =
password =
topic_prefix = picframe
discovery_prefix = homeassistant
node_id =
```

## 6. Behaviour

| Rule | Decision |
|---|---|
| Who can play | Anyone on the Wi-Fi. YouTube TV-code pairing also allows anyone who can see the code. AirPlay can have a password. |
| Takeover | The latest source wins. Lock from Home Assistant to prevent it. |
| Volume | The PipeWire output volume is set to the cap; each phone's volume scales below it. New sessions start at `start_volume`. |
| Quiet hours | 22:00–07:00 by default: the cap drops to `quiet_max_volume`. Music is allowed. |
| Frame at night | Music does **not** wake the display. The now-playing card shows only while the screen is on. |
| Idle | The YouTube queue is cleared after 30 minutes without playback. |
| DAC unplugged mid-song | Playback continues on the headphone jack (`output = auto`), and moves back when the DAC is re-plugged. |
| Built-in audio | Left **enabled**, because the jack is a supported output. The add-on sets the output explicitly, so Raspberry Pi OS's HDMI default never wins. |

## 7. Frame integration (phase 3)

### 7.1 Now-playing card (the only core change)
- The add-on writes `$XDG_RUNTIME_DIR/picframe/nowplaying.json` atomically, and deletes it when playback stops:
  ```json
  {"state": "playing", "source": "youtube", "title": "…", "artist": "…",
   "album": "…", "artwork": "/run/user/1000/picframe/artwork.jpg", "updated": 1760000000}
  ```
- The frame watches that path. While the file exists and is fresh, it shows a small card in a corner: artwork, title, artist and a source icon. New core options, off when the add-on is absent: `show_now_playing = yes` and `now_playing_position`.
- When idle, it optionally shows a **"Play music here"** hint with the speaker name and the YouTube TV code.

### 7.2 Home Assistant
Home Assistant's MQTT discovery has no media-player entity type, so the add-on exposes these simpler entities under its own device ("<name> Speaker"):

| Entity | Type |
|---|---|
| Now playing | sensor (attributes: source, artist, album) |
| Source | sensor (`youtube`, `spotify`, `airplay`, `idle`) |
| Volume | number (0 to `max_volume`) |
| Stop | button |
| Lock | switch |

## 8. Updates

- `update-music.sh` updates everything; the YouTube parts are the ones that need it.
- **yt-dlp nightly**, when `auto_update_ytdlp = yes`: a systemd timer installs the latest official yt-dlp release into the add-on's own environment, after checking its checksum. The previous version is kept and restored automatically if a smoke test fails. Debian's packaged yt-dlp is **not** used, because it lags behind YouTube's changes.
- **Trust note:** the owner accepted that the Pi downloads and runs new yt-dlp code nightly from yt-dlp's GitHub releases.
- `yt-cast-receiver` and the other Node dependencies are pinned in `package-lock.json`, and updated deliberately with `update-music.sh`.

## 9. Repository layout

```
addons/music/
  install-music.sh            # installs packages, user services, linger, default config
  uninstall-music.sh          # removes everything the add-on added; never touches the frame
  update-music.sh
  music.ini                   # default config (migration template)
  youtube-receiver/           # Node.js: yt-cast-receiver → mpv + yt-dlp
    package.json, package-lock.json, receiver.js, test/
  coordinator/                # Python: takeover, volume, output, idle, now-playing, MQTT
    coordinator.py, tests/
  systemd/                    # user units: music-youtube, music-spotify, music-airplay,
                              # music-coordinator, ytdlp-update.timer
  README.md
```

CI: ShellCheck for the scripts, Node tests for the receiver, pytest for the coordinator (on Trixie), and Ruff.

## 10. Prototype checks (before phase 1)

Each check takes about an hour on the target Pi 3 with Trixie. Results go back into this document.

| # | Check | If it fails |
|---|---|---|
| P1 | Multiple phones in one session, sharing a queue, from the **YouTube Music** app | Single sender only, still useful |
| P2 | TV-code (manual) pairing supported by `yt-cast-receiver` | Local-network discovery only; document the guest Wi-Fi requirement |
| P3 | Current yt-dlp plays YouTube Music audio reliably | YouTube on Android falls back to Music Assistant or Bluetooth; AirPlay covers iPhone |
| P4 | Debian's shairport-sync has AirPlay 2 | Use AirPlay 1, or build with AirPlay 2 support |
| P5 | Pi 3 headroom: receiver, mpv and slideshow together; no audio glitches during photo fades | Lower the slideshow's priority further, or `transition_ms = 0` |
| P6 | DAC ↔ jack switching on plug and unplug, with WirePlumber | The coordinator enforces the output itself |
| P7 | Coordinator can stop each source (YouTube pause, AirPlay D-Bus, librespot restart) | Fall back to stopping the losing service |

## 11. Phases and acceptance

**Phase 1: audio output and the YouTube receiver**
- `install-music.sh` on a fresh Trixie Pi 3, with the frame installed, completes without errors.
- An Android and an iPhone running YouTube Music both see "Living Room" and play through the DAC. Unplugging the DAC moves playback to the jack.
- The volume never exceeds `max_volume`, and new sessions start at `start_volume`.
- The services restart after being killed, and the frame is unaffected throughout.

**Phase 2: Spotify, AirPlay and the coordinator**
- A Spotify Premium guest and an AirPlay iPhone can each play.
- Starting a second source stops the first. Locking prevents takeover.
- Quiet-hours cap, idle clearing and config hot-reload work.

**Phase 3: frame and Home Assistant integration**
- The now-playing card appears within about 2 s of a track starting and disappears when playback stops. Without the add-on, the frame behaves exactly as before.
- The Home Assistant entities appear with no YAML and work: stop, volume, lock.
- The "Play music here" hint, with the TV code if P2 passed.

## 12. Out of scope

Multi-room or synchronised audio · a real Chromecast receiver (no third-party receivers allowed) · local or NAS music libraries · Bluetooth audio (possible future add-on) · router guest-Wi-Fi configuration (documented, not automated).

## 13. Risks

| Risk | Mitigation |
|---|---|
| YouTube changes break the receiver | Nightly yt-dlp with rollback; AirPlay and Music Assistant fallbacks; clearly labelled as liable to break |
| Guest Wi-Fi isolation hides the Pi | Document router settings; TV-code pairing for YouTube (P2) |
| Pi 3 CPU contention | Audio runs at higher priority; the slideshow fade can be disabled (P5) |
| Headphone-jack noise | The USB DAC is the preferred output; the jack is the fallback |
| Two sources at once | The coordinator enforces a single active source |
