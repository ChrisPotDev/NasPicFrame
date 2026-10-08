# Music add-on

Turns the picture frame's Pi into a speaker that guests can play to from their phones, with no app to install and no account on the Pi:

| | Android | iPhone |
|---|---|---|
| **YouTube Music / YouTube** | Cast button → "Living Room" | Cast button → "Living Room" |
| **Spotify** (Premium) | Spotify's device list → "Living Room" | Spotify's device list → "Living Room" |
| **Anything else** | n/a | AirPlay → "Living Room" |

The add-on is **optional and separate from the frame**: the frame works without it, and it works without the frame. Design decisions and background are in [docs/music-addon-plan.md](../../docs/music-addon-plan.md).

> **YouTube casting is unofficial.** It uses the same protocol as YouTube on smart TVs (DIAL and the Lounge API), reverse-engineered by the open-source [yt-cast-receiver](https://github.com/patrickkfkan/yt-cast-receiver), and plays audio through [yt-dlp](https://github.com/yt-dlp/yt-dlp). YouTube changes break it from time to time; yt-dlp is updated nightly, so it usually fixes itself within days. Playing YouTube streams outside YouTube's apps is against YouTube's terms of service. Spotify Connect and AirPlay are stable.

## Requirements

- Raspberry Pi OS **Trixie** (64-bit, desktop), the same as the frame. Bookworm is best-effort.
- A **USB DAC** (recommended) or the **headphone jack**, connected to your amplifier.
- Phones on the **same Wi-Fi** as the Pi. Many routers isolate devices on the *guest* Wi-Fi from each other, which hides the Pi. Either let guests use the main Wi-Fi, or enable your router's option to let guest devices see each other. YouTube also works across networks with **"Link with TV code"**; the frame shows the code.

## Install

From the repository folder on the Pi (the same clone as the frame):

```bash
sudo ./addons/music/install-music.sh
```

It installs `mpv`, Node.js, `shairport-sync`, `raspotify` (Spotify Connect, from its official repository), and yt-dlp in a private Python environment. It then sets up user services that start at boot. If the frame has MQTT configured, the broker settings are copied over. To **upgrade**, run `git pull` and the same command again. Your `music.ini` is migrated, never replaced.

To remove it: `sudo ./addons/music/uninstall-music.sh` (add `--purge` to delete `music.ini` too). The frame isn't affected either way.

## Settings: `/etc/picframe/music.ini`

```bash
sudo nano /etc/picframe/music.ini
```

| Setting | Default | |
|---|---|---|
| `name` | `Living Room` | Name on guests' phones, in all three apps. `youtube_name` / `spotify_name` / `airplay_name` override it per app. |
| `youtube`, `spotify`, `airplay` | `yes` | Turn each receiver on or off. |
| `airplay_password` | empty | Empty = anyone on the Wi-Fi. |
| `output` | `auto` | `auto` = USB DAC if plugged in, else the headphone jack (follows unplugging and re-plugging). Or `usb`, `headphones`, `hdmi`. |
| `max_volume` | `80` | Ceiling for guests' volume. Set your amplifier so 100% would be the loudest you'd ever want. |
| `start_volume` | `40` | Each new session starts here. |
| `quiet_hours` / `quiet_max_volume` | `22:00-07:00` / `50` | Lower ceiling at night. Music is still allowed. |
| `allow_takeover` | `yes` | A new guest can take over what's playing. |
| `idle_clear_minutes` | `30` | Clear the YouTube queue after this long without playback. |
| `auto_update_ytdlp` | `yes` | Update yt-dlp nightly (with rollback). |

Changes take effect within about 5 seconds; the receivers restart with the new settings.

## How it behaves

- **One source at a time.** If someone starts Spotify while YouTube is playing, YouTube pauses. If an iPhone AirPlays during Spotify, the Spotify guest's app shows *disconnected*. Spotify and AirPlay have no "pause this guest" command, so the session is ended instead.
- **Several phones, one YouTube queue.** Guests casting from YouTube Music all add to the same queue, as with a smart TV.
- **Volume.** Each phone's volume slider controls 0% up to `max_volume` (or `quiet_max_volume` at night). The cap is applied to the output itself, so nothing can exceed it.
- **On the frame:** while music plays, a small card shows the artwork, title, artist and app (top-right). When nothing is playing, a one-line hint shows the speaker name and the YouTube TV code. Turn these off with `show_now_playing = no` / `show_music_hint = no` in the frame's `config.ini`. Music doesn't wake the screen at night.

## Home Assistant

With `[mqtt] enabled = yes`, a device called **"Living Room Speaker"** appears automatically:

| Entity | Type | |
|---|---|---|
| Now playing | sensor | "Title – Artist"; the attributes hold the source, album and state |
| Source | sensor | `youtube`, `spotify`, `airplay` or `idle` |
| Volume | number | 0 to `max_volume` |
| Stop | button | Stops whatever is playing |
| Lock | switch | While on, new guests can't take over |

## Troubleshooting

All commands are run as the desktop user, not with sudo:

```bash
systemctl --user status 'music-*'            # are the services running?
journalctl --user -u 'music-*' -f            # live logs
pactl list short sinks                       # audio outputs PipeWire can see
~/.local/share/picframe-music/venv/bin/yt-dlp --version
/opt/picframe-music/update-ytdlp.sh          # update yt-dlp now
```

| Symptom | Fix |
|---|---|
| The Pi doesn't appear on a phone | Is the phone on the same Wi-Fi (not an isolated guest network)? For YouTube, try "Link with TV code". |
| YouTube connects but nothing plays | yt-dlp can't fetch the audio. Run `/opt/picframe-music/update-ytdlp.sh`; if it still fails, YouTube has changed and a yt-dlp fix usually follows within days. AirPlay keeps working meanwhile. |
| Sound comes from the screen, not the amp | Set `output = usb` or `headphones`. Check `pactl list short sinks` lists the DAC. |
| Hiss or crackle on the headphone jack | Expected on the Pi 3's jack; use a USB DAC. |
| Two songs at once | Check `journalctl --user -u music-coordinator` and report it: the coordinator should stop one of them. |

## Not verified on hardware yet

The code is tested in CI, but these depend on the real Pi, phones and network; see the plan's [prototype checks](../../docs/music-addon-plan.md#10-prototype-checks-before-phase-1):

1. Several phones in one YouTube Music session sharing the queue.
2. "Link with TV code" pairing.
3. yt-dlp fetching YouTube Music audio reliably on the Pi 3.
4. AirPlay 2 or AirPlay 1 from Debian's shairport-sync.
5. No audio glitches during photo crossfades on a Pi 3.
6. Switching between the DAC and the jack when plugging or unplugging.
7. The coordinator stopping each source on takeover.
