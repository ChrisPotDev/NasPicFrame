#!/usr/bin/env bash
# Show the music add-on's logs: coordinator, receivers and yt-dlp updates.
# Any arguments go to journalctl, e.g.:  music-logs -f   or   music-logs -n 200
#
# (`journalctl --user` finds nothing on Raspberry Pi OS: its journal is kept in
# memory, so user services log to the system journal instead.)
set -euo pipefail

matches=()
for unit in music-coordinator music-youtube music-spotify music-airplay ytdlp-update; do
    matches+=("_SYSTEMD_USER_UNIT=$unit.service")
done

# Members of adm / systemd-journal can read the system journal without sudo.
if id -nG | grep -qwE 'adm|systemd-journal'; then
    exec journalctl --no-pager -o short "$@" "${matches[@]}"
fi
exec sudo journalctl --no-pager -o short "$@" "${matches[@]}"
