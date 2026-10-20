#!/usr/bin/env bash
# Install or update yt-dlp (fetches YouTube audio for the cast receiver) in the
# add-on's own Python environment. If the new version fails a quick test, the
# previous version is put back.
#
#   update-ytdlp.sh           install / update now (run as the desktop user, not root)
#   update-ytdlp.sh --timer   nightly run: skipped if auto_update_ytdlp = no
set -euo pipefail
trap 'echo "update-ytdlp.sh: failed at line $LINENO (exit $?)" >&2' ERR

VENV="$HOME/.local/share/picframe-music/venv"
CONFIG="${MUSIC_CONFIG_FILE:-/etc/picframe/music.ini}"
# "Me at the zoo", YouTube's first video: tiny, and not going anywhere.
TEST_URL="https://www.youtube.com/watch?v=jNQXAC9IVRw"

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as the desktop user, not root/sudo (it installs into ~/.local)." >&2
    exit 1
fi

if [ "${1:-}" = "--timer" ] && [ -r "$CONFIG" ] &&
    grep -Eiq '^[[:space:]]*auto_update_ytdlp[[:space:]]*=[[:space:]]*(no|false|off|0)[[:space:]]*$' "$CONFIG"; then
    echo "Automatic yt-dlp updates are off (auto_update_ytdlp = no)"
    exit 0
fi

pip() { "$VENV/bin/python" -m pip --disable-pip-version-check --quiet "$@"; }
# Empty when not installed. (`pip show` fails then; that must not end the script.)
version() { { "$VENV/bin/python" -m pip show yt-dlp 2>/dev/null || true; } | awk '/^Version:/ {print $2}'; }
smoke_test() {
    timeout 180 "$VENV/bin/yt-dlp" --quiet --no-warnings --js-runtimes node \
        -f bestaudio --get-url "$TEST_URL" >/dev/null
}

if [ ! -x "$VENV/bin/python" ]; then
    echo "Creating Python environment in $VENV"
    mkdir -p "$(dirname "$VENV")"
    python3 -m venv "$VENV"
fi

previous="$(version)"
echo "Installing/updating yt-dlp (currently: ${previous:-not installed})"
if ! pip install --upgrade "yt-dlp[default]"; then
    echo "pip could not install yt-dlp - is the Pi online? (pypi.org must be reachable)" >&2
    exit 1
fi
current="$(version)"

if [ "${YTDLP_SKIP_TEST:-}" = "1" ]; then   # CI: GitHub's servers are blocked by YouTube
    echo "yt-dlp $current installed (test skipped)"
    exit 0
fi

if ! curl -fsS --max-time 10 -o /dev/null https://www.youtube.com; then
    echo "yt-dlp $current installed; YouTube not reachable right now, so not tested" >&2
    exit 0
fi

if smoke_test; then
    if [ "$previous" = "$current" ]; then
        echo "yt-dlp $current is up to date and working"
    else
        echo "yt-dlp updated: ${previous:-none} -> $current, working"
    fi
    exit 0
fi

if [ -n "$previous" ] && [ "$previous" != "$current" ]; then
    echo "yt-dlp $current failed the test - restoring $previous" >&2
    pip install "yt-dlp[default]==$previous"
    smoke_test && exit 0
fi
echo "yt-dlp $current is installed but can't fetch YouTube audio right now. To see why, run:" >&2
echo "  $VENV/bin/yt-dlp --js-runtimes node -f bestaudio --get-url $TEST_URL" >&2
exit 1
