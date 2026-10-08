#!/usr/bin/env bash
# Install or update yt-dlp (fetches YouTube audio for the cast receiver) in the
# add-on's own Python environment. If the new version fails a quick test, the
# previous version is put back.
#
#   update-ytdlp.sh           update now
#   update-ytdlp.sh --timer   nightly run: skipped if auto_update_ytdlp = no
set -euo pipefail

VENV="$HOME/.local/share/picframe-music/venv"
CONFIG="${MUSIC_CONFIG_FILE:-/etc/picframe/music.ini}"
# "Me at the zoo", YouTube's first video: tiny, and not going anywhere.
TEST_URL="https://www.youtube.com/watch?v=jNQXAC9IVRw"

if [ "${1:-}" = "--timer" ] && [ -r "$CONFIG" ] &&
    grep -Eiq '^[[:space:]]*auto_update_ytdlp[[:space:]]*=[[:space:]]*(no|false|off|0)[[:space:]]*$' "$CONFIG"; then
    echo "Automatic yt-dlp updates are off (auto_update_ytdlp = no)"
    exit 0
fi

if ! curl -fsS --max-time 10 -o /dev/null https://www.youtube.com; then
    echo "YouTube not reachable - skipping update" >&2
    exit 0
fi

[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
pip() { "$VENV/bin/python" -m pip --disable-pip-version-check --quiet "$@"; }
version() { "$VENV/bin/python" -m pip show yt-dlp 2>/dev/null | awk '/^Version:/ {print $2}'; }
smoke_test() {
    timeout 180 "$VENV/bin/yt-dlp" --quiet --no-warnings --js-runtimes node \
        -f bestaudio --get-url "$TEST_URL" >/dev/null
}

previous="$(version)"
pip install --upgrade "yt-dlp[default]"
current="$(version)"

if smoke_test; then
    if [ "$previous" = "$current" ]; then
        echo "yt-dlp $current is up to date"
    else
        echo "yt-dlp updated: ${previous:-none} -> $current"
    fi
    exit 0
fi

if [ -n "$previous" ] && [ "$previous" != "$current" ]; then
    echo "yt-dlp $current failed the test - restoring $previous" >&2
    pip install "yt-dlp[default]==$previous"
    smoke_test && exit 0
fi
echo "yt-dlp $current can't fetch YouTube audio right now. YouTube may have changed;" \
     "a fixed yt-dlp usually follows within days and will be installed automatically." >&2
exit 1
