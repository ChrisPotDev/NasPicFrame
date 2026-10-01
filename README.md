# NasPicFrame

A Raspberry Pi picture frame that runs on its own. It boots straight into a fullscreen, shuffled slideshow of photos from an SMB/CIFS share on a NAS, and switches the display off at night.

- **Portrait pairing:** two portrait photos are shown side by side, instead of one with wide empty borders.
- **Blurred matting:** any space a photo doesn't fill shows a blurred, darkened copy of the photo, not black bars.
- **EXIF overlay:** the date and place each photo was taken appear in its corner, along with an on-screen clock.
- **Home Assistant:** the frame shows up as a device over MQTT. From there you can turn the display on or off, go to the next or previous photo, pause, and toggle the clock and photo info.

| File | Goes to | Purpose |
|---|---|---|
| `picframe.py` | `/opt/picframe/` | Slideshow, rendering, display power and MQTT (Pygame, Pillow, paho-mqtt) |
| `config.ini` | `/etc/picframe/` | Photo folder, delay, wake and sleep times, display method |
| `picframe.service` | `/etc/systemd/system/` | Starts at boot, waits for the network and the share, restarts after a crash |
| `fstab.example` | appended to `/etc/fstab` | Mounts the SMB share at `/mnt/photos` |
| `nas-credentials.example` | `/etc/samba/nas-credentials` | NAS login, readable by root only |
| `install.sh` | run once | Installs everything above |

## How it works

```
boot ─► network-online.target ─► desktop autologin (labwc, Wayland)
                                         │
       /mnt/photos (systemd automount) ◄─┤
                                         ▼
                          picframe.service (runs as your user)
                          ExecStartPre: wait for compositor + share
                          ExecStart:    picframe.py
                             ├─ scanner thread: walks the share, shuffles photos
                             ├─ loader thread:  picks the next photo (+ a portrait partner),
                             │                  reads EXIF, renders matte + layout ahead of time
                             ├─ MQTT thread:    Home Assistant discovery, commands → queue
                             └─ main loop:      shows slides, crossfades, clock/captions,
                                                schedule + MQTT commands
                                                └─ wlopm --off/--on '*' for display power
```

The slideshow only reads from the NAS on background threads. If the NAS drops off the network, the current photo stays on screen, and the wake and sleep times and MQTT control keep working. When the share comes back, the slideshow picks up again on its own.

---

## Step 0: Prepare the Pi

1. Flash **Raspberry Pi OS (64-bit) with desktop**, Bookworm or newer, using Raspberry Pi Imager. In the Imager settings, set the user name, Wi-Fi, locale/**timezone** and enable SSH.
   *Use the desktop image, not Lite.* On recent Pi OS the desktop already runs a Wayland compositor (labwc), which handles HDMI modes, hot-plug and display power for you.
2. Boot the Pi, SSH in, and update it:
   ```bash
   sudo apt update && sudo apt full-upgrade -y
   ```
3. Check that the compositor is **labwc**. It is the default since late 2024; older installs used wayfire:
   `sudo raspi-config` → *Advanced Options* → *Wayland* → **W3 labwc**.
4. Check the timezone, because wake and sleep times use local time:
   `timedatectl`. To change it: `sudo raspi-config` → *Localisation* → *Timezone*.

## Step 1: Copy the files and run the installer

From your PC (or with `git clone` on the Pi):

```bash
scp -r NasPicFrame <user>@<pi-address>:~/
ssh <user>@<pi-address>
cd ~/NasPicFrame
sed -i 's/\r$//' install.sh      # only needed if the files went through Windows
chmod +x install.sh
sudo ./install.sh
```

The installer:
- installs `python3-pygame python3-pil cifs-utils wlr-randr wlopm`
- copies the app to `/opt/picframe` and the config to `/etc/picframe/config.ini`. If a config already exists, it is kept and only new options are added to it (see *Upgrading* below)
- creates `/mnt/photos` and `/etc/samba/nas-credentials` (mode 600)
- installs and enables `picframe.service`, with your user name and UID filled in
- sets **boot to desktop with autologin** (`raspi-config nonint do_boot_behaviour B4`)
- **turns off the OS's own screen blanking** (`do_blanking 1`), so only the schedule turns the screen off
- enables `NetworkManager-wait-online.service`, so `network-online.target` means the network is really up

## Step 2: Mount the network share (`/etc/fstab`)

### 2a. Credentials

```bash
sudo nano /etc/samba/nas-credentials
```
```
username=photoframe
password=your-password
domain=WORKGROUP
```
```bash
sudo chown root:root /etc/samba/nas-credentials
sudo chmod 600 /etc/samba/nas-credentials
```
A read-only NAS account just for the frame is a good idea.

### 2b. The fstab line

Get your UID and GID with `id`. They are usually `1000`. Then add this **single line** to `/etc/fstab` (`sudo nano /etc/fstab`):

```
//192.168.1.10/Photos  /mnt/photos  cifs  credentials=/etc/samba/nas-credentials,uid=1000,gid=1000,ro,vers=3.0,iocharset=utf8,file_mode=0444,dir_mode=0555,soft,_netdev,nofail,x-systemd.automount,x-systemd.mount-timeout=30,x-systemd.after=network-online.target  0  0
```

| Option | Why |
|---|---|
| `//192.168.1.10/Photos` | Use the NAS's **IP address**. Name lookup (`nas.local`) may not work yet this early in boot. Give the NAS a fixed IP or a DHCP reservation. Write a space in the share name as `\040`. |
| `credentials=` | Keeps the password out of the world-readable `/etc/fstab`. |
| `uid=,gid=` | Files appear owned by the frame user. |
| `ro`, `file_mode=0444`, `dir_mode=0555` | The frame never writes, so it can't damage your photos. |
| `vers=3.0` | Modern SMB. Use `3.1.1` for current NAS firmware, or `2.1` for very old units. Never `1.0`. |
| `iocharset=utf8` | Accented and non-Latin file names work. |
| `soft` | An unreachable server returns an error instead of hanging programs forever. |
| `_netdev` | Marks it as a network filesystem, so it is ordered after the network. |
| `nofail` | **The Pi still boots** if the NAS is off. |
| `x-systemd.automount` | Boot never waits on the NAS. The share mounts the first time something reads `/mnt/photos`, and mounts again on the next access after an outage. This is what makes it reliable. |
| `x-systemd.mount-timeout=30` | A dead NAS blocks a mount attempt for at most 30 s. |
| `x-systemd.after=network-online.target` | Mount attempts wait until the network is really up. |

### 2c. Test it

```bash
sudo systemctl daemon-reload          # regenerate mount units from fstab
sudo systemctl restart remote-fs.target
ls /mnt/photos | head                 # first access triggers the mount
findmnt /mnt/photos                   # should show type cifs
```
If it fails, `sudo dmesg | tail` and `journalctl -u mnt-photos.mount` show the reason. Usually it's a wrong password (`-13`), a wrong share name (`-2`), or an SMB version mismatch (`-95`; change `vers=`).

On Wi-Fi, turn off power saving. It is the most common cause of a share that keeps dropping:
```bash
nmcli -t -f NAME connection show --active        # find the connection name
sudo nmcli connection modify "<name>" 802-11-wireless.powersave 2
```

## Step 3: Configure the slideshow

```bash
sudo nano /etc/picframe/config.ini
```

```ini
[slideshow]
image_dir = /mnt/photos
delay = 30

[schedule]
wake_time = 07:00
sleep_time = 22:00
```

Every option is explained in the file. It has these sections: `[slideshow]`, `[overlay]`, `[schedule]`, `[display]` and `[mqtt]`. **The app checks the file every 5 seconds and restarts itself when it changes**, so you don't need to restart the service.

### Local photos as well as the network share

`image_dir` takes several folders, either separated by commas or one per indented line:

```ini
image_dir = /mnt/photos
    /home/pi/Pictures
    /media/pi/USBSTICK/Photos
```

- All the folders feed **one shuffle**, so each photo is equally likely to come up. A folder with more photos therefore appears more often.
- **Local photos keep showing when the NAS is down.** A folder that comes back empty or unreachable is rescanned every 30 seconds, and its photos rejoin the shuffle when it's back.
- At boot, the service waits up to 120 s for *all* the folders, so the first shuffle includes the NAS. Local folders are ready immediately.
- **Local folders:** copy photos to `~/Pictures` (or any folder) with `scp`, a USB stick, or straight onto the SD card. The service runs as your user, so the folder must be readable by that user.
- **USB sticks:** the desktop mounts them automatically at `/media/<user>/<LABEL>`. If you give the stick a fixed label, the path stays the same each time. When the stick is removed, its photos drop out at the next rescan, and plugging it back in brings them back.
- To use **only** local photos, set `image_dir` to the local folder and leave the fstab line out.

### Upgrading

Copy the new files to the Pi and run `sudo ./install.sh` again. It never replaces your config. Instead it **migrates** it:

- Any section or option that is new in this version is added to `/etc/picframe/config.ini` with its explanatory comments and default value. A new option goes at the end of its section; a new section goes at the end of the file.
- **Existing values, comments, and options you've commented out are never changed.** The file is merged as text rather than rewritten by `configparser`, which would remove every comment.
- Before changing anything, the installer saves a backup as `config.ini.bak-YYYYMMDD-HHMMSS`. If nothing is missing, the file isn't touched.
- The running slideshow is restarted so the new code takes effect.

Example output: `/etc/picframe/config.ini: added [slideshow] pair_portraits, [slideshow] matting, …, [overlay], [mqtt] (backup: …)`

To run the migration on its own, for example after restoring an old config:
```bash
sudo python3 /opt/picframe/picframe.py --config /etc/picframe/config.ini \
     --migrate-config /opt/picframe/config.default.ini
```
Missing options always fall back to built-in defaults anyway, so an old config never stops the frame from starting. The migration just makes new options visible and documented in your file.

What the slideshow does:
- Fullscreen and borderless (`pygame.FULLSCREEN | NOFRAME`). The cursor is hidden twice: `set_visible(False)` plus a transparent cursor.
- **Shuffle without repeats.** Every photo is shown once before any photo repeats, then the list is shuffled again, never showing the same photo twice in a row.
- Turns photos upright using the camera's EXIF rotation data. JPEGs are decoded at reduced resolution (`Image.draft`), which makes 24 MP photos load several times faster on a Pi.
- Loads the next photo while the current one is showing, then crossfades to it.
- Skips Synology/QNAP thumbnail, recycle and snapshot folders (`@eaDir`, `#recycle`, ...).
- If there are no photos to show, it shows a **placeholder screen** with *"Waiting for photos in /mnt/photos ..."* and keeps retrying. The built-in placeholder is a dark dusk gradient with a soft photo icon. It is drawn in code, so it's sharp at any resolution and there's no image file to ship. To use your own picture instead, set `placeholder_image = /home/pi/placeholder.jpg`. Keep it on the Pi, not on the share, since the share may be what's unavailable. Your picture is displayed like any other photo (fitted, with the blurred background), with the status text along the bottom. If it can't be read, the built-in placeholder is used.
- Keeps the last 100 slides so you can step back through them with *previous*. A pair comes back as the same pair.

### Portrait pairing (`pair_portraits`)

The app first checks how each photo is oriented. It reads only the file header and applies the EXIF rotation, so the check is cheap, and the result is cached. When the shuffle picks a portrait photo and the screen is landscape, the app looks for another portrait among the **next 30 photos in the shuffle**. It takes that photo out of the shuffle, so it isn't shown again later in the round, and shows the two side by side with `pair_gap` pixels between them. If no partner is found, for example at the end of a round, the portrait is shown alone on its blurred mat. On a portrait-mounted screen this flips: two landscape photos are stacked one above the other.

### Blurred matting (`matting = blur`)

Any space a photo doesn't cover is filled with a version of the same photo. The photo is cropped to fill the area, blurred heavily (`mat_blur`), darkened (`mat_brightness`), and the sharp photo is placed on top. To keep this cheap on a Pi, the blur is done at 1/10 resolution and then enlarged; at this blur strength it looks the same. Each half of a portrait pair gets its own mat. All rendering happens on the loader thread while the previous photo is still on screen, so transitions stay smooth. `matting = color` gives plain `background`-coloured bars instead.

### EXIF caption and clock (`[overlay]`)

- **Date:** read from the EXIF *DateTimeOriginal* tag, falling back to *DateTime*, and formatted with `date_format`.
- **Place:** read from the EXIF GPS data. By default (`geocode = no`) it shows coordinates, and nothing leaves the Pi. With `geocode = nominatim` it shows *"Town, Country"* from OpenStreetMap's Nominatim service. Coordinates are rounded to about 100 m before they are sent, requests stay under Nominatim's limit of one per second, and results are cached in `~/.cache/picframe/geocode.json`, so each place is looked up only once.
- The caption sits in the bottom-right corner **of the photo itself**; each photo in a pair has its own caption. The clock is in the bottom-left corner of the screen. Both use slightly translucent text with a soft shadow, so they are readable on any photo without drawing attention.
- Photos without EXIF data, such as screenshots or many photos saved from messaging apps, just get no caption.

## Step 4: Display power management

At `sleep_time` the app blanks its window and powers the display output off. At `wake_time` it turns the output on again. Power-off happens on the Pi side: the HDMI signal stops, so almost every monitor or TV drops into standby. This saves power and prevents burn-in. Schedules that cross midnight work (`wake 18:00 / sleep 01:00`). If the Pi reboots during the night, the display goes off again within a second of the app starting.

### Which method, and why

`method = auto` picks the first that applies:

| Method | When | Notes |
|---|---|---|
| **`wlopm --off '*'`** | Wayland (labwc/wayfire): **current Pi OS** | **Recommended.** Uses the *wlr-output-power-management* protocol, which is real DPMS. The output stays configured, so the fullscreen window survives the night untouched. Pi OS's own screen blanking uses this tool. |
| `wlr-randr --output HDMI-A-1 --off` | Wayland, if wlopm is missing | Disables the output completely. Works, but the compositor treats it as unplugged. The app recreates its fullscreen window after waking to handle this. |
| `xset dpms force off` | X11 session (legacy Pi OS / "X11" in raspi-config) | DPMS through the X server. |
| `vcgencmd display_power 0` | Legacy firmware display driver only | **Does nothing on current Pi OS.** Since Bookworm the KMS driver (`vc4-kms-v3d`) owns the display, not the firmware. Kept only for old installs. |
| `cec` | Set it manually | Sends HDMI-CEC *standby* / *image-view-on*. Use it if your frame is a **TV**, because some TVs show "No signal" instead of going to standby. Needs `sudo apt install v4l-utils` and a CEC-capable TV. |

Test it by hand. Run this over SSH as the desktop user:
```bash
XDG_RUNTIME_DIR=/run/user/$(id -u) python3 /opt/picframe/picframe.py --display off
XDG_RUNTIME_DIR=/run/user/$(id -u) python3 /opt/picframe/picframe.py --display on
```

**If you'd rather use cron than the built-in schedule:** leave `wake_time` and `sleep_time` empty in the config (display always on), then add to `crontab -e`:
```
0 22 * * *  XDG_RUNTIME_DIR=/run/user/1000 /usr/bin/python3 /opt/picframe/picframe.py --display off
0 7  * * *  XDG_RUNTIME_DIR=/run/user/1000 /usr/bin/python3 /opt/picframe/picframe.py --display on
```
The built-in schedule is still the better choice. It also pauses the slideshow at night, so the Pi doesn't read photos from the NAS, and it puts the right state back after a reboot.

## Step 5: Start automatically (systemd)

`/etc/systemd/system/picframe.service` (installed by `install.sh` with your user and UID filled in):

```ini
[Unit]
Description=NasPicFrame photo slideshow
Wants=network-online.target
After=network-online.target remote-fs.target systemd-user-sessions.service
StartLimitIntervalSec=0

[Service]
Type=simple
User=pi
Group=pi
Environment=XDG_RUNTIME_DIR=/run/user/1000
Environment=WAYLAND_DISPLAY=wayland-0
Environment=DISPLAY=:0
Environment=PYTHONUNBUFFERED=1
ExecStartPre=/usr/bin/python3 /opt/picframe/picframe.py --config /etc/picframe/config.ini --wait --wait-timeout 120
ExecStart=/usr/bin/python3 /opt/picframe/picframe.py --config /etc/picframe/config.ini
Restart=always
RestartSec=10
TimeoutStartSec=180
TimeoutStopSec=15
Nice=5

[Install]
WantedBy=graphical.target
```

Design notes:
- **Waits for the network.** `Wants=` and `After=network-online.target` work together with `NetworkManager-wait-online.service`, which the installer enables.
- **Waits for the mount.** `After=remote-fs.target` orders the service after the share's automount is set up. `ExecStartPre … --wait` then reads `/mnt/photos`, which triggers the mount, and waits until it has content or 120 s pass. The same step waits for the desktop compositor's socket. If the socket isn't named `wayland-0`, the app finds the actual one.
- **Why not `RequiresMountsFor=/mnt/photos`?** If the NAS is off at boot, that dependency fails, and **`Restart=` does not retry dependency failures**, so the frame would stay dark until someone rebooted it. Here the service always starts, and the app shows the "waiting" screen until the NAS appears.
- **Restarts after crashes.** `Restart=always` with `RestartSec=10` and `StartLimitIntervalSec=0` means systemd never gives up.
- It runs as **your user** inside the autologin desktop session (`XDG_RUNTIME_DIR` / `WAYLAND_DISPLAY`). That is what lets it draw on the screen and call `wlopm`.

## Step 6: Home Assistant (MQTT)

1. In Home Assistant, install a broker if you don't have one: *Settings → Add-ons → Mosquitto broker*. Then add the **MQTT** integration. Create a Home Assistant user for the frame, for example `picframe`; the Mosquitto add-on accepts Home Assistant users as MQTT logins.
2. On the Pi, edit `/etc/picframe/config.ini`:
   ```ini
   [mqtt]
   enabled = yes
   host = 192.168.1.20
   username = picframe
   password = your-password
   ```
3. Save the file. The app restarts itself within 5 seconds, and **"Picture Frame"** appears under *Settings → Devices → MQTT* with no YAML needed:

| Entity | Type | Does |
|---|---|---|
| Display | switch | Turns the screen on or off (same mechanism as the schedule). |
| Pause slideshow | switch | Stops advancing; *Next*/*Previous* still work. |
| Clock | switch | Shows or hides the on-screen clock. |
| Photo info | switch | Shows or hides the date and place captions. |
| Next photo / Previous photo | button | Steps forward, or back through history. |
| Current photo | sensor | File name(s) on screen; the attributes hold the full paths and captions. |

The device shows as *unavailable* when the frame is off or disconnected: an MQTT last-will message marks it offline. All states are published retained. Discovery is sent again whenever the broker reconnects or Home Assistant restarts, because the app listens for Home Assistant's `homeassistant/status` birth message. If the broker is down at boot, the app keeps retrying in the background.

**Manual vs scheduled display power:** a *Display* command from Home Assistant lasts until the next scheduled switch. Turning the frame on at 23:00 keeps it on until the next `sleep_time`; turning it off at 15:00 keeps it off until the next `wake_time`. For presence-based control, leave `wake_time`/`sleep_time` empty and automate it in Home Assistant instead. Entity IDs depend on the device name, so check yours in Home Assistant:

```yaml
automation:
  - alias: "Picture frame follows presence"
    trigger:
      - platform: state
        entity_id: binary_sensor.living_room_occupancy
        for: { minutes: 10 }
    action:
      - service: "switch.turn_{{ 'on' if trigger.to_state.state == 'on' else 'off' }}"
        target: { entity_id: switch.picture_frame_display }
```

Raw topics, for other MQTT clients (`<node>` is the hostname, or `node_id` if set):

```
picframe/<node>/display/set     ON | OFF        picframe/<node>/display/state
picframe/<node>/pause/set       ON | OFF        picframe/<node>/pause/state
picframe/<node>/clock/set       ON | OFF        picframe/<node>/clock/state
picframe/<node>/info/set        ON | OFF        picframe/<node>/info/state
picframe/<node>/next/set        PRESS
picframe/<node>/previous/set    PRESS
picframe/<node>/photo/state, …/photo/attributes, …/availability (online | offline)
```
For example: `mosquitto_pub -h 192.168.1.20 -u picframe -P … -t picframe/livingroom/next/set -m PRESS`

## Daily use and troubleshooting

```bash
journalctl -u picframe -f            # live log: photo count, skips, display on/off
sudo systemctl restart picframe      # restart
sudo systemctl stop picframe         # stop (also turns the display back on)
systemctl status picframe mnt-photos.automount mnt-photos.mount
```

| Symptom | Fix |
|---|---|
| `No Wayland/X11 display` in the log | Desktop autologin is off: `sudo raspi-config nonint do_boot_behaviour B4`, then reboot. |
| Desktop or taskbar flashes during boot | Normal. It lasts the few seconds before the service starts. |
| Display never turns off | `journalctl -u picframe \| grep Display`. Check that `wlopm` is installed, or set `method = wlr-randr`. |
| Screen goes black during the day | Something else is blanking it. Run `sudo raspi-config nonint do_blanking 1` and reboot. |
| Window not fullscreen / wrong monitor (native Wayland) | Add `Environment=SDL_VIDEODRIVER=x11` (or `wayland`) under `[Service]` and test both. |
| Slow on very large PNG/HEIC files | Export JPEGs at about 2–4K for the frame. JPEG decoding uses the fast reduced-size path. |
| Photos sideways | They lack EXIF orientation data. Rotate the files themselves. |
| Frame doesn't appear in Home Assistant | `journalctl -u picframe \| grep MQTT`. If the connection is refused, check the username and password. Check that `discovery_prefix` matches Home Assistant's setting. |
| Few portrait pairs | Partners come from the next 30 photos in the shuffle. A library with very few portraits pairs less often. |
| Captions show coordinates, not places | Set `geocode = nominatim`; the Pi needs internet access. |

Testing with a keyboard attached: **→/←** next/previous, **Space** pause, **C** clock, **I** info, **D** display, **Esc** quit (systemd restarts it).

Requirements: Raspberry Pi 3B+ / 4 / 5 / Zero 2 W, Raspberry Pi OS Bookworm or newer, Python 3.9+.
