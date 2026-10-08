#!/usr/bin/env python3
"""Start one music receiver with settings from music.ini (run by its systemd unit).

    launch.py youtube|spotify|airplay [-c CONFIG]
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from musicconf import DEFAULT_CONFIG, Settings  # noqa: E402

INSTALL_DIR = os.path.dirname(os.path.abspath(__file__))
YOUTUBE_CONTROL_PORT = 8766  # localhost only: lets the coordinator pause YouTube


def runtime_dir():
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    path = os.path.join(base, "picframe-music")
    os.makedirs(path, exist_ok=True)
    return path


def ytdlp_path():
    return os.path.expanduser("~/.local/share/picframe-music/venv/bin/yt-dlp")


def _conf_string(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def shairport_config(settings, runtime):
    """shairport-sync config: name, PulseAudio/PipeWire output, metadata pipe."""
    general = [f"name = {_conf_string(settings.names['airplay'])};",
               'output_backend = "pa";']
    if settings.airplay_password:
        general.append(f"password = {_conf_string(settings.airplay_password)};")
    pipe = os.path.join(runtime, "shairport-metadata")
    return "\n".join([
        "general = {", *("  " + g for g in general), "};",
        "sessioncontrol = {",
        '  allow_session_interruption = "yes";',  # a second iPhone can take over
        "  session_timeout = 20;",
        "};",
        "metadata = {",
        '  enabled = "yes";',
        '  include_cover_art = "yes";',
        f"  pipe_name = {_conf_string(pipe)};",
        "  pipe_timeout = 5000;",
        "};",
        'pa = { application_name = "picframe-airplay"; };',
        "",
    ])


def build_command(source, settings, runtime):
    """Returns (argv, extra environment) for one receiver."""
    if source == "spotify":
        return ([
            "librespot",
            "--name", settings.names["spotify"],
            "--device-type", "speaker",
            "--backend", "pulseaudio",
            "--bitrate", "320",
            "--initial-volume", str(settings.start_volume),
            "--enable-volume-normalisation",
            "--disable-audio-cache",
            "--onevent", os.path.join(INSTALL_DIR, "spotify_event.py"),
        ], {"PULSE_PROP": "application.name=picframe-spotify"})

    if source == "airplay":
        conf = os.path.join(runtime, "shairport-sync.conf")
        with open(conf, "w", encoding="utf-8") as f:
            f.write(shairport_config(settings, runtime))
        os.chmod(conf, 0o600)  # may contain the AirPlay password
        return (["shairport-sync", "-c", conf],
                {"PULSE_PROP": "application.name=picframe-airplay"})

    if source == "youtube":
        return (["node", os.path.join(INSTALL_DIR, "youtube-receiver", "receiver.js")], {
            "MUSIC_CONFIG": json.dumps(settings.as_json()),
            "MUSIC_RUNTIME_DIR": runtime,
            "YTDLP_PATH": ytdlp_path(),
            "CONTROL_PORT": str(YOUTUBE_CONTROL_PORT),
        })

    raise SystemExit(f"Unknown source {source!r}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", choices=("youtube", "spotify", "airplay"))
    ap.add_argument("-c", "--config", default=DEFAULT_CONFIG)
    args = ap.parse_args()

    argv, env = build_command(args.source, Settings(args.config), runtime_dir())
    os.environ.update(env)
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main()
