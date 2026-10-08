#!/usr/bin/env python3
"""Configuration for the NasPicFrame music add-on: parsing, migration on upgrade,
and pre-filling MQTT settings from the frame's config.

Usage:
    musicconf.py -c CONFIG --json                    print effective settings as JSON
    musicconf.py -c CONFIG --migrate TEMPLATE        add options new in TEMPLATE
    musicconf.py -c CONFIG --prefill-mqtt FRAME_INI  copy MQTT broker settings from the frame
"""

import argparse
import configparser
import datetime as dt
import json
import os
import re
import shutil
import socket
import sys

DEFAULT_CONFIG = "/etc/picframe/music.ini"

DEFAULTS = {
    "music": {
        "name": "Living Room",
        "youtube_name": "", "spotify_name": "", "airplay_name": "",
        "youtube": "yes", "spotify": "yes", "airplay": "yes",
        "airplay_password": "",
    },
    "output": {"output": "auto", "usb_device": ""},
    "volume": {
        "max_volume": "80", "start_volume": "40",
        "quiet_hours": "22:00-07:00", "quiet_max_volume": "50",
    },
    "behaviour": {"allow_takeover": "yes", "idle_clear_minutes": "30"},
    "updates": {"auto_update_ytdlp": "yes"},
    "mqtt": {
        "enabled": "no", "host": "homeassistant.local", "port": "1883",
        "username": "", "password": "",
        "topic_prefix": "picframe", "discovery_prefix": "homeassistant", "node_id": "",
    },
}

OUTPUTS = ("auto", "usb", "headphones", "hdmi")
SOURCES = ("youtube", "spotify", "airplay")


def _percent(section, key):
    return max(0, min(100, section.getint(key)))


def parse_quiet_hours(value):
    """'22:00-07:00' -> (time(22), time(7)); empty -> None."""
    value = value.strip()
    if not value:
        return None
    m = re.fullmatch(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", value)
    if not m:
        raise SystemExit(f"quiet_hours must look like 22:00-07:00, not {value!r}")
    start, end = (dt.datetime.strptime(t, "%H:%M").time() for t in m.groups())
    return start, end


def in_period(period, now):
    """True if `now` is inside (start, end); handles periods crossing midnight."""
    if period is None:
        return False
    start, end = period
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end


class Settings:
    def __init__(self, path):
        cp = configparser.ConfigParser(interpolation=None)  # passwords may contain '%'
        cp.read_dict(DEFAULTS)
        if not cp.read(path, encoding="utf-8"):
            raise SystemExit(f"Config file not found: {path}")
        m, o, v, b, u, q = (cp[k] for k in ("music", "output", "volume", "behaviour",
                                            "updates", "mqtt"))
        self.path = path
        self.name = m.get("name").strip() or "Living Room"
        self.names = {s: (m.get(f"{s}_name").strip() or self.name) for s in SOURCES}
        self.enabled = {s: m.getboolean(s) for s in SOURCES}
        self.airplay_password = m.get("airplay_password").strip()

        self.output = o.get("output").strip().lower()
        if self.output not in OUTPUTS:
            raise SystemExit(f"output must be one of {OUTPUTS}, not {self.output!r}")
        self.usb_device = o.get("usb_device").strip()

        self.max_volume = _percent(v, "max_volume")
        self.start_volume = _percent(v, "start_volume")
        self.quiet_hours = parse_quiet_hours(v.get("quiet_hours"))
        self.quiet_max_volume = _percent(v, "quiet_max_volume")

        self.allow_takeover = b.getboolean("allow_takeover")
        self.idle_clear_minutes = max(0, b.getint("idle_clear_minutes"))
        self.auto_update_ytdlp = u.getboolean("auto_update_ytdlp")

        self.mqtt_enabled = q.getboolean("enabled")
        self.mqtt_host = q.get("host").strip()
        self.mqtt_port = q.getint("port")
        self.mqtt_username = q.get("username").strip()
        self.mqtt_password = q.get("password")
        self.mqtt_topic_prefix = q.get("topic_prefix").strip().strip("/")
        self.mqtt_discovery_prefix = q.get("discovery_prefix").strip().strip("/")
        self.mqtt_node_id = re.sub(r"[^A-Za-z0-9_-]", "_",
                                   q.get("node_id").strip() or socket.gethostname())

    def volume_cap(self, now=None):
        now = now or dt.datetime.now().time()
        if in_period(self.quiet_hours, now):
            return min(self.max_volume, self.quiet_max_volume)
        return self.max_volume

    def as_json(self):
        return {
            "names": self.names,
            "enabled": self.enabled,
            "start_volume": self.start_volume,
            "idle_clear_minutes": self.idle_clear_minutes,
        }


# --------------------------------------------------------------------------- migration
# Same text-based merge as the frame's picframe.py (kept as a copy so the add-on
# works without the frame installed). Comments and values are never changed.

_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]")
_KEY_RE = re.compile(r"^([^\s;#=:\[][^=:]*?)\s*[=:]")


def _is_comment(line):
    return line.lstrip().startswith((";", "#"))


def ini_layout(lines):
    sections, current = {}, None
    for i, line in enumerate(lines):
        m = _SECTION_RE.match(line)
        if m:
            current = sections[m.group(1).strip()] = {"start": i, "end": i + 1, "keys": {}}
        elif current is not None and line.strip() and not _is_comment(line):
            current["end"] = i + 1
            k = _KEY_RE.match(line)
            if k:
                current["keys"][k.group(1).strip().lower()] = i
    return sections


def _comment_start(lines, i):
    while i > 0 and _is_comment(lines[i - 1]):
        i -= 1
    return i


def _write_with_backup(path, lines):
    backup = f"{path}.bak-{dt.datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(path, backup)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    shutil.copymode(path, tmp)
    os.replace(tmp, path)
    return backup


def migrate_config(path, template_path):
    with open(template_path, encoding="utf-8") as f:
        template = f.read().splitlines()
    with open(path, encoding="utf-8") as f:
        config = f.read().splitlines()
    tpl_layout, cfg_layout = ini_layout(template), ini_layout(config)

    inserts, appended, added = {}, [], []
    for name, tpl in tpl_layout.items():
        cfg = cfg_layout.get(name)
        if cfg is None:
            appended += [""] + template[_comment_start(template, tpl["start"]):tpl["end"]]
            added.append(f"[{name}]")
            continue
        for key, i in tpl["keys"].items():
            if key not in cfg["keys"]:
                inserts.setdefault(cfg["end"], []).extend(
                    [""] + template[_comment_start(template, i):i + 1])
                added.append(f"[{name}] {key}")

    if not added:
        print(f"{path}: up to date")
        return 0
    out = []
    for i, line in enumerate(config):
        out.extend(inserts.get(i, []))
        out.append(line)
    out.extend(inserts.get(len(config), []))
    out.extend(appended)
    backup = _write_with_backup(path, out)
    print(f"{path}: added {', '.join(added)} (backup: {backup})")
    return 0


def set_options(path, section, values):
    """Set existing options in one section, keeping every other line as it is."""
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    layout = ini_layout(lines).get(section)
    if layout is None:
        return False
    for key, value in values.items():
        i = layout["keys"].get(key)
        if i is not None:
            lines[i] = f"{key} = {value}"
    _write_with_backup(path, lines)
    return True


def prefill_mqtt(path, frame_config):
    """Copy broker settings from the frame's config, if MQTT is enabled there and
    hasn't been configured here yet."""
    frame = configparser.ConfigParser(interpolation=None)
    if not frame.read(frame_config, encoding="utf-8") or not frame.has_section("mqtt"):
        print("No frame MQTT settings to copy")
        return 0
    fm = frame["mqtt"]
    if not fm.getboolean("enabled", fallback=False):
        print("MQTT is not enabled in the frame's config; nothing copied")
        return 0
    if Settings(path).mqtt_enabled:
        print("MQTT already configured for music; left unchanged")
        return 0
    keys = ("host", "port", "username", "password", "topic_prefix", "discovery_prefix")
    values = {k: fm.get(k, fallback=DEFAULTS["mqtt"][k]) for k in keys}
    values["enabled"] = "yes"
    set_options(path, "mqtt", values)
    print(f"Copied MQTT settings from {frame_config}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="NasPicFrame music add-on configuration")
    ap.add_argument("-c", "--config", default=DEFAULT_CONFIG)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--json", action="store_true", help="print effective settings as JSON")
    group.add_argument("--migrate", metavar="TEMPLATE")
    group.add_argument("--prefill-mqtt", metavar="FRAME_INI")
    args = ap.parse_args()

    if args.migrate:
        return migrate_config(args.config, args.migrate)
    if args.prefill_mqtt:
        return prefill_mqtt(args.config, args.prefill_mqtt)
    print(json.dumps(Settings(args.config).as_json()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
