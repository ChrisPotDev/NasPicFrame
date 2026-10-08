#!/usr/bin/env bash
# Remove the NasPicFrame music add-on. The picture frame itself is not touched.
#     sudo ./addons/music/uninstall-music.sh           keep /etc/picframe/music.ini
#     sudo ./addons/music/uninstall-music.sh --purge   remove it too
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo $0" >&2
    exit 1
fi
FRAME_USER="${SUDO_USER:-}"
if [ -z "$FRAME_USER" ] || [ "$FRAME_USER" = "root" ]; then
    echo "Run via sudo from the user the add-on was installed for." >&2
    exit 1
fi
FRAME_UID="$(id -u "$FRAME_USER")"
FRAME_HOME="$(getent passwd "$FRAME_USER" | cut -d: -f6)"
UNIT_DIR="$FRAME_HOME/.config/systemd/user"

as_user() {
    sudo -u "$FRAME_USER" XDG_RUNTIME_DIR="/run/user/$FRAME_UID" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$FRAME_UID/bus" "$@"
}

echo "==> Stopping services"
as_user systemctl --user disable --now music-coordinator.service ytdlp-update.timer 2>/dev/null || true
as_user systemctl --user stop music-youtube.service music-spotify.service music-airplay.service 2>/dev/null || true
for unit in music-coordinator.service music-youtube.service music-spotify.service \
            music-airplay.service ytdlp-update.service ytdlp-update.timer; do
    rm -f "$UNIT_DIR/$unit"
done
as_user systemctl --user daemon-reload || true

echo "==> Removing files"
rm -rf /opt/picframe-music "$FRAME_HOME/.local/share/picframe-music" "$FRAME_HOME/.local/state/picframe-music"
rm -rf "/run/user/$FRAME_UID/picframe-music"
rm -f "/run/user/$FRAME_UID/picframe/nowplaying.json" "/run/user/$FRAME_UID/picframe/music-hint.json"
if [ "${1:-}" = "--purge" ]; then
    rm -f /etc/picframe/music.ini /etc/picframe/music.ini.bak-*
    echo "    removed /etc/picframe/music.ini"
else
    echo "    kept /etc/picframe/music.ini (use --purge to remove it)"
fi

cat <<EOF

Done. Packages were left installed in case anything else uses them. To remove them too:
  sudo apt remove raspotify shairport-sync mpv nqptp
  sudo rm /etc/apt/sources.list.d/raspotify.list /usr/share/keyrings/raspotify_key.asc
EOF
