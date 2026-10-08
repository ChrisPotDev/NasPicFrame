#!/usr/bin/env bash
# NasPicFrame music add-on installer. Run on the Pi as the desktop user, via sudo:
#     sudo ./addons/music/install-music.sh
# Safe to re-run to upgrade: music.ini is migrated, never replaced.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo $0" >&2
    exit 1
fi
FRAME_USER="${SUDO_USER:-}"
if [ -z "$FRAME_USER" ] || [ "$FRAME_USER" = "root" ]; then
    echo "Run via sudo from the user that logs in to the desktop (not as root)." >&2
    exit 1
fi
FRAME_UID="$(id -u "$FRAME_USER")"
FRAME_GROUP="$(id -gn "$FRAME_USER")"
FRAME_HOME="$(getent passwd "$FRAME_USER" | cut -d: -f6)"
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST=/opt/picframe-music
CONFIG=/etc/picframe/music.ini

# Run a command as the desktop user, inside their systemd/PipeWire session.
as_user() {
    sudo -u "$FRAME_USER" XDG_RUNTIME_DIR="/run/user/$FRAME_UID" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$FRAME_UID/bus" "$@"
}

# shellcheck source=/dev/null
. /etc/os-release
case "${VERSION_CODENAME:-}" in
    trixie) ;;
    bookworm) echo "Note: built for Raspberry Pi OS Trixie; Bookworm is best-effort." ;;
    *) echo "Warning: untested on ${PRETTY_NAME:-this OS}. Raspberry Pi OS Trixie is recommended." ;;
esac

echo "==> Installing packages"
apt-get update
apt-get install -y mpv nodejs npm python3-venv python3-paho-mqtt pulseaudio-utils \
    shairport-sync avahi-daemon curl ca-certificates
# AirPlay 2 timing helper; only needed (and only packaged) where shairport-sync has AirPlay 2.
apt-get install -y nqptp 2>/dev/null || echo "    (nqptp not available - AirPlay 1 only, fine for one room)"

echo "==> Spotify Connect (raspotify)"
if ! dpkg -s raspotify >/dev/null 2>&1; then
    install -d /usr/share/keyrings
    curl -fsSL https://dtcooper.github.io/raspotify/key.asc -o /usr/share/keyrings/raspotify_key.asc
    echo "deb [signed-by=/usr/share/keyrings/raspotify_key.asc] https://dtcooper.github.io/raspotify raspotify main" \
        > /etc/apt/sources.list.d/raspotify.list
    apt-get update
    apt-get install -y raspotify
fi
# librespot and shairport-sync run as *user* services instead, so they share the
# desktop's PipeWire audio with the other receivers.
systemctl disable --now raspotify.service 2>/dev/null || true
systemctl disable --now shairport-sync.service 2>/dev/null || true

echo "==> Installing add-on to $DEST"
install -d "$DEST/youtube-receiver"
install -m 755 "$SRC"/coordinator.py "$SRC"/launch.py "$SRC"/musicconf.py \
    "$SRC"/spotify_event.py "$SRC"/update-ytdlp.sh "$DEST/"
install -m 644 "$SRC"/youtube-receiver/package.json "$SRC"/youtube-receiver/package-lock.json \
    "$SRC"/youtube-receiver/receiver.js "$SRC"/youtube-receiver/player.js \
    "$SRC"/youtube-receiver/mpv.js "$SRC"/youtube-receiver/util.js "$DEST/youtube-receiver/"
install -m 644 "$SRC/music.ini" "$DEST/music.default.ini"
# Files edited on Windows may have CRLF line endings; strip them.
sed -i 's/\r$//' "$DEST"/*.py "$DEST"/*.sh "$DEST/music.default.ini"
(cd "$DEST/youtube-receiver" && npm ci --omit=dev --no-audit --no-fund)

echo "==> Configuration"
install -d /etc/picframe
if [ -f "$CONFIG" ]; then
    echo "    existing config: adding new options only, your values are kept"
    python3 "$DEST/musicconf.py" -c "$CONFIG" --migrate "$DEST/music.default.ini"
else
    install -m 640 "$DEST/music.default.ini" "$CONFIG"
    if [ -f /etc/picframe/config.ini ]; then
        python3 "$DEST/musicconf.py" -c "$CONFIG" --prefill-mqtt /etc/picframe/config.ini
    fi
fi
# Readable by the services (they run as $FRAME_USER) but not by everyone: it can hold
# the AirPlay and MQTT passwords.
chown "root:$FRAME_GROUP" "$CONFIG"
chmod 640 "$CONFIG"

echo "==> User services"
loginctl enable-linger "$FRAME_USER"   # user services run from boot, before/without login
systemctl start "user@$FRAME_UID.service"
UNIT_DIR="$FRAME_HOME/.config/systemd/user"
sudo -u "$FRAME_USER" mkdir -p "$UNIT_DIR"
for unit in "$SRC"/systemd/*; do
    install -m 644 -o "$FRAME_USER" -g "$FRAME_GROUP" "$unit" "$UNIT_DIR/"
    sed -i 's/\r$//' "$UNIT_DIR/$(basename "$unit")"
done
as_user systemctl --user daemon-reload

echo "==> yt-dlp"
as_user "$DEST/update-ytdlp.sh" || echo "    yt-dlp test failed - YouTube casting may not work until the next update"

as_user systemctl --user enable music-coordinator.service ytdlp-update.timer
as_user systemctl --user restart music-coordinator.service
as_user systemctl --user start ytdlp-update.timer

cat <<EOF

Done. The speaker is called "$(python3 "$DEST/musicconf.py" -c "$CONFIG" --json \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["names"]["youtube"])')".

  Settings:  sudo nano $CONFIG        (name, output, volume caps, ...)
  Logs:      journalctl --user -u 'music-*' -f        (run as $FRAME_USER)
  Status:    systemctl --user status 'music-*'

Phones must be on the same Wi-Fi as the Pi (YouTube also works with "Link with TV code").
EOF
