#!/usr/bin/env python3
"""librespot --onevent hook: records what Spotify is playing for the coordinator.

librespot runs this on every player event, with details in environment variables
(PLAYER_EVENT, NAME, ARTISTS, ALBUM, COVERS, ...).
"""

import json
import os
import sys
import time


def state_path():
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.path.join(base, "picframe-music", "spotify.json")


def apply_event(state, env):
    """Returns the new state dict, or None when the session has ended."""
    event = env.get("PLAYER_EVENT", "")
    if event in ("stopped", "session_disconnected"):
        return None
    state = dict(state or {})
    if event == "track_changed":
        artists = [a for a in env.get("ARTISTS", "").splitlines() if a]
        covers = [c for c in env.get("COVERS", "").splitlines() if c]
        state.update({
            "title": env.get("NAME", ""),
            # Podcast episodes have no artists; show the show's name instead.
            "artist": ", ".join(artists) or env.get("SHOW_NAME", ""),
            "album": env.get("ALBUM", ""),
            "artwork_url": covers[0] if covers else "",
        })
    elif event in ("playing", "paused"):
        state["state"] = event
    state["updated"] = time.time()
    return state


def main():
    path = state_path()
    try:
        with open(path, encoding="utf-8") as f:
            current = json.load(f)
    except (OSError, ValueError):
        current = None

    new = apply_event(current, os.environ)
    if new is None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(new, f)
    os.replace(tmp, path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
