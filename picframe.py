#!/usr/bin/env python3
"""NasPicFrame: a fullscreen, randomised photo slideshow for a Raspberry Pi
picture frame.

Features: shuffled slideshow from a NAS share, portrait pairing, blurred matting,
EXIF date/location captions, on-screen clock, scheduled display power-off, and
MQTT control with Home Assistant discovery.

Usage:
    picframe.py [-c CONFIG]                   run the slideshow
    picframe.py [-c CONFIG] --wait            wait until the display session and photo
                                              share are ready (systemd ExecStartPre)
    picframe.py [-c CONFIG] --display on|off  switch the screen and exit (testing / cron)
    picframe.py [-c CONFIG] --migrate-config TEMPLATE
                                              add options new in TEMPLATE to CONFIG (upgrades)

Keyboard (for testing): Right/Left next/previous, Space pause, C clock, I info,
D display, Esc/Q quit.
"""

import argparse
import collections
import configparser
import datetime as dt
import glob
import json
import logging
import math
import os
import queue
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
# A fullscreen SDL window minimises itself when it loses focus (e.g. to a desktop
# notification), which would expose the desktop. Keep it up.
os.environ.setdefault("SDL_VIDEO_MINIMIZE_ON_FOCUS_LOSS", "0")

import pygame  # noqa: E402 - must follow the SDL environment settings above
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps  # noqa: E402

VERSION = "2.0"
log = logging.getLogger("picframe")

DEFAULT_CONFIG = "/etc/picframe/config.ini"

DEFAULTS = {
    "slideshow": {
        "image_dir": "/mnt/photos",
        "delay": "30",
        "recursive": "yes",
        "extensions": ".jpg, .jpeg, .png, .webp, .bmp, .gif",
        "fit": "contain",
        "transition_ms": "800",
        "background": "black",
        "rescan_minutes": "60",
        "placeholder_image": "",
        "pair_portraits": "yes",
        "pair_gap": "8",
        "matting": "blur",
        "mat_blur": "40",
        "mat_brightness": "0.45",
    },
    "overlay": {
        "show_clock": "yes",
        "show_info": "yes",
        "clock_format": "%H:%M",
        "date_format": "%-d %B %Y",
        "font": "",
        "geocode": "no",
        "geocode_language": "en",
    },
    "schedule": {
        "wake_time": "07:00",
        "sleep_time": "22:00",
    },
    "display": {
        "method": "auto",
        "output": "auto",
        "cec_device": "/dev/cec0",
    },
    "mqtt": {
        "enabled": "no",
        "host": "homeassistant.local",
        "port": "1883",
        "username": "",
        "password": "",
        "topic_prefix": "picframe",
        "discovery_prefix": "homeassistant",
        "device_name": "Picture Frame",
        "node_id": "",
    },
}

# Folders NAS appliances use for thumbnails, recycle bins and snapshots.
SKIP_DIRS = {"@eadir", "#recycle", "@recycle", "#snapshot", "$recycle.bin", ".thumbnails"}

# Give up on a slide whose read has hung this long (NAS gone away mid-read).
LOAD_TIMEOUT = 60
# After this many unreadable photos in a row, back off instead of spinning.
MAX_FAILURES = 5
# How many upcoming photos to examine when looking for a portrait partner.
PAIR_WINDOW = 30
# Slides remembered for "previous".
HISTORY_SIZE = 100

# EXIF tags / IFDs
TAG_ORIENTATION = 0x0112
TAG_DATETIME = 0x0132
TAG_DATETIME_ORIGINAL = 0x9003
IFD_EXIF = 0x8769
IFD_GPS = 0x8825

RESTART = "restart"
_resampling = getattr(Image, "Resampling", Image)
RESAMPLE = _resampling.LANCZOS
FAST = _resampling.BILINEAR
SMOOTH = _resampling.BICUBIC
_frombytes = getattr(pygame.image, "frombytes", None) or pygame.image.fromstring

Photo = collections.namedtuple("Photo", "path image taken gps")
Panel = collections.namedtuple("Panel", "rect caption")       # rect = where the photo landed
Slide = collections.namedtuple("Slide", "paths image panels")  # image = full-screen PIL canvas


# --------------------------------------------------------------------------- config

def parse_hhmm(value):
    value = value.strip()
    if not value:
        return None
    return dt.datetime.strptime(value, "%H:%M").time()


def _choice(section, key, allowed):
    value = section.get(key).strip().lower()
    if value not in allowed:
        raise SystemExit(f"{key} must be one of {allowed}, not {value!r}")
    return value


class Settings:
    def __init__(self, path):
        # No interpolation: date/clock formats and passwords contain '%'.
        cp = configparser.ConfigParser(interpolation=None)
        cp.read_dict(DEFAULTS)
        if not cp.read(path, encoding="utf-8"):
            raise SystemExit(f"Config file not found: {path}")
        s, o, sch, d, m = (cp[k] for k in ("slideshow", "overlay", "schedule", "display", "mqtt"))

        self.path = path
        # One or more folders, separated by commas or on indented continuation lines.
        self.image_dirs = list(dict.fromkeys(
            d.strip() for d in re.split(r"[,\n]", s.get("image_dir")) if d.strip()))
        if not self.image_dirs:
            raise SystemExit("image_dir must name at least one folder")
        self.delay = max(1.0, s.getfloat("delay"))
        self.recursive = s.getboolean("recursive")
        self.extensions = tuple(
            e if e.startswith(".") else "." + e
            for e in (x.strip().lower() for x in s.get("extensions").split(","))
            if e
        )
        self.fit = _choice(s, "fit", ("contain", "cover"))
        self.transition_ms = max(0, s.getint("transition_ms"))
        self.background = s.get("background").strip()
        self.rescan_seconds = max(1.0, s.getfloat("rescan_minutes")) * 60
        self.placeholder_image = s.get("placeholder_image").strip()
        self.pair_portraits = s.getboolean("pair_portraits")
        self.pair_gap = max(0, s.getint("pair_gap"))
        self.matting = _choice(s, "matting", ("blur", "color"))
        self.mat_blur = max(0, s.getint("mat_blur"))
        self.mat_brightness = min(1.0, max(0.0, s.getfloat("mat_brightness")))

        self.show_clock = o.getboolean("show_clock")
        self.show_info = o.getboolean("show_info")
        self.clock_format = o.get("clock_format")
        self.date_format = o.get("date_format")
        self.font = o.get("font").strip()
        self.geocode = _choice(o, "geocode", ("no", "nominatim"))
        self.geocode_language = o.get("geocode_language").strip()

        self.wake_time = parse_hhmm(sch.get("wake_time"))
        self.sleep_time = parse_hhmm(sch.get("sleep_time"))

        self.display_method = d.get("method").strip().lower()
        self.display_output = d.get("output").strip()
        self.cec_device = d.get("cec_device").strip()

        self.mqtt_enabled = m.getboolean("enabled")
        self.mqtt_host = m.get("host").strip()
        self.mqtt_port = m.getint("port")
        self.mqtt_username = m.get("username").strip()
        self.mqtt_password = m.get("password")
        self.mqtt_topic_prefix = m.get("topic_prefix").strip().strip("/")
        self.mqtt_discovery_prefix = m.get("discovery_prefix").strip().strip("/")
        self.mqtt_device_name = m.get("device_name").strip()
        self.mqtt_node_id = re.sub(r"[^A-Za-z0-9_-]", "_",
                                   m.get("node_id").strip() or socket.gethostname())


# --------------------------------------------------------------------------- config migration
# Text-based on purpose: configparser would drop every comment when writing back.

_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]")
_KEY_RE = re.compile(r"^([^\s;#=:\[][^=:]*?)\s*[=:]")


def _is_comment(line):
    return line.lstrip().startswith((";", "#"))


def _ini_layout(lines):
    """section -> {"start": header line, "end": one past its last option line,
    "keys": {key: line}}. Comments and blank lines are left alone."""
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
    """Index of the first comment line directly above line i (its documentation)."""
    while i > 0 and _is_comment(lines[i - 1]):
        i -= 1
    return i


def migrate_config(path, template_path):
    """Add sections and options that exist in the template but not in the config,
    together with their comments. Existing values and comments are never changed;
    the file is only rewritten (with a backup) if something is missing."""
    with open(template_path, encoding="utf-8") as f:
        template = f.read().splitlines()
    with open(path, encoding="utf-8") as f:
        config = f.read().splitlines()
    tpl_layout, cfg_layout = _ini_layout(template), _ini_layout(config)

    inserts = {}    # config line index -> lines to insert before it
    appended = []   # whole new sections, added at the end
    added = []
    for name, tpl in tpl_layout.items():
        cfg = cfg_layout.get(name)
        if cfg is None:
            appended += [""] + template[_comment_start(template, tpl["start"]):tpl["end"]]
            added.append(f"[{name}]")
            continue
        for key, i in tpl["keys"].items():
            if key not in cfg["keys"]:
                block = template[_comment_start(template, i):i + 1]
                inserts.setdefault(cfg["end"], []).extend([""] + block)
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

    backup = f"{path}.bak-{dt.datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(path, backup)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    shutil.copymode(path, tmp)
    os.replace(tmp, path)
    print(f"{path}: added {', '.join(added)} (backup: {backup})")
    return 0


def is_awake(wake, sleep, now):
    """True if `now` is inside the display-on period. Handles periods crossing midnight."""
    if wake is None or sleep is None or wake == sleep:
        return True
    if wake < sleep:
        return wake <= now < sleep
    return now >= wake or now < sleep


def file_mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


# --------------------------------------------------------------------------- session

def fix_session_env():
    """Make sure WAYLAND_DISPLAY names a socket that exists (labwc uses wayland-0,
    wayfire wayland-1). Returns True once a Wayland or X11 display is reachable."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    os.environ["XDG_RUNTIME_DIR"] = runtime

    current = os.environ.get("WAYLAND_DISPLAY")
    if current and os.path.exists(os.path.join(runtime, current)):
        return True
    sockets = sorted(p for p in glob.glob(os.path.join(runtime, "wayland-*"))
                     if not p.endswith(".lock"))
    if sockets:
        os.environ["WAYLAND_DISPLAY"] = os.path.basename(sockets[0])
        return True

    # No Wayland compositor: fall back to an X11 session if there is one.
    os.environ.pop("WAYLAND_DISPLAY", None)
    display = os.environ.setdefault("DISPLAY", ":0")
    return os.path.exists("/tmp/.X11-unix/X" + display.lstrip(":").split(".")[0])


def dir_has_entries(path):
    try:
        with os.scandir(path) as it:  # also triggers an x-systemd.automount
            return any(True for _ in it)
    except OSError:
        return False


def wait_until_ready(settings, timeout):
    deadline = time.monotonic() + timeout

    while not fix_session_env():
        if time.monotonic() > deadline:
            log.error("No Wayland/X11 display after %ds - is desktop autologin enabled?", timeout)
            return 1
        time.sleep(2)
    log.info("Display session ready (%s)",
             os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))

    # Wait for all folders (the network share especially), so the first shuffle
    # includes everything; local folders are ready immediately.
    while missing := [d for d in settings.image_dirs if not dir_has_entries(d)]:
        if time.monotonic() > deadline:
            # Start anyway: the slideshow uses what it has and keeps retrying the rest.
            log.warning("%s still empty or unreachable after %ds - starting anyway",
                        ", ".join(missing), timeout)
            return 0
        time.sleep(3)
    log.info("Photo directories ready: %s", ", ".join(settings.image_dirs))
    return 0


# --------------------------------------------------------------------------- display power

class DisplayPower:
    METHODS = ("wlopm", "wlr-randr", "xset", "vcgencmd", "cec", "none")

    def __init__(self, method="auto", output="auto", cec_device="/dev/cec0"):
        self.output = output
        self.cec_device = cec_device
        self.method = self._detect() if method == "auto" else method
        if self.method not in self.METHODS:
            raise SystemExit(f"Unknown display method {self.method!r}; use one of {self.METHODS}")
        log.info("Display power method: %s", self.method)

    @staticmethod
    def _detect():
        if os.environ.get("WAYLAND_DISPLAY"):
            # wlopm = DPMS-style power off; the output stays configured, so windows
            # keep their place. wlr-randr disables the output entirely.
            for tool in ("wlopm", "wlr-randr"):
                if shutil.which(tool):
                    return tool
            log.warning("Wayland session but neither wlopm nor wlr-randr is installed")
        elif os.environ.get("DISPLAY") and shutil.which("xset"):
            return "xset"
        if shutil.which("vcgencmd"):
            return "vcgencmd"  # only works with the legacy (non-KMS) display driver
        return "none"

    @property
    def reopen_after_wake(self):
        # A disabled-then-re-enabled wlr-randr output is a "new" output: the fullscreen
        # window has to be recreated on it.
        return self.method == "wlr-randr"

    def set(self, on):
        for cmd in self._commands(on):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                if r.returncode != 0:
                    log.warning("%s failed (%d): %s", " ".join(cmd), r.returncode, r.stderr.strip())
            except (OSError, subprocess.TimeoutExpired) as e:
                log.warning("%s failed: %s", " ".join(cmd), e)
        log.info("Display %s (%s)", "on" if on else "off", self.method)

    def _commands(self, on):
        m = self.method
        if m == "wlopm":
            return [["wlopm", "--on" if on else "--off",
                     "*" if self.output == "auto" else self.output]]
        if m == "wlr-randr":
            return [["wlr-randr", "--output", o, "--on" if on else "--off"]
                    for o in self._wlr_outputs()]
        if m == "xset":
            if on:  # wake, then disable X's own blanking so it stays on all day
                return [["xset", "dpms", "force", "on"], ["xset", "s", "off"], ["xset", "-dpms"]]
            return [["xset", "+dpms"], ["xset", "dpms", "force", "off"]]
        if m == "vcgencmd":
            return [["vcgencmd", "display_power", "1" if on else "0"]]
        if m == "cec":
            return [["cec-ctl", "-d", self.cec_device, "--playback"],
                    ["cec-ctl", "-d", self.cec_device, "--to", "0",
                     "--image-view-on" if on else "--standby"]]
        return []

    def _wlr_outputs(self):
        if self.output != "auto":
            return [self.output]
        try:
            out = subprocess.run(["wlr-randr"], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("wlr-randr failed: %s", e)
            return []
        # Output names are the unindented lines, e.g. 'HDMI-A-1 "Dell Inc. ..."'
        return [line.split()[0] for line in out.splitlines() if line and not line[0].isspace()]


# --------------------------------------------------------------------------- photo library

class ImageLibrary:
    """Keeps the photo list fresh on a background thread and hands photos out in
    shuffled order: every photo is shown once before any repeats."""

    def __init__(self, settings):
        self.roots = settings.image_dirs
        self.extensions = settings.extensions
        self.recursive = settings.recursive
        self.rescan_seconds = settings.rescan_seconds
        self._files = []
        self._bag = []
        self._last = None
        self._portrait = {}  # path -> bool, from file headers
        self._lock = threading.Lock()
        threading.Thread(target=self._run, name="scanner", daemon=True).start()

    def _run(self):
        while True:
            files, all_found = [], True
            for root in self.roots:
                found = self._scan(root)
                log.info("Found %d photos in %s", len(found), root)
                files.extend(found)
                all_found = all_found and bool(found)
            files = list(dict.fromkeys(files))  # overlapping folders: show each file once
            with self._lock:
                if set(files) != set(self._files):
                    self._bag = []  # reshuffle so new photos show up promptly
                self._files = files
            # A folder came back empty (NAS down?): retry soon instead of waiting the
            # full interval, so its photos rejoin the shuffle once it's back.
            time.sleep(self.rescan_seconds if all_found else 30)

    def _wanted(self, name):
        return not name.startswith(".") and name.lower().endswith(self.extensions)

    def _scan(self, root):
        found = []
        try:
            if self.recursive:
                def onerror(err):
                    log.warning("Cannot read %s: %s", err.filename, err.strerror)
                for dirpath, dirnames, filenames in os.walk(root, onerror=onerror):
                    dirnames[:] = [d for d in dirnames
                                   if not d.startswith(".") and d.lower() not in SKIP_DIRS]
                    found.extend(os.path.join(dirpath, f) for f in filenames if self._wanted(f))
            else:
                with os.scandir(root) as it:
                    found = [e.path for e in it if e.is_file() and self._wanted(e.name)]
        except OSError as e:
            log.warning("Cannot scan %s: %s", root, e)
        return found

    def next(self):
        with self._lock:
            if not self._files:
                return None
            if not self._bag:
                self._bag = self._files[:]
                random.shuffle(self._bag)
                # Don't show the same photo twice in a row across a reshuffle.
                if len(self._bag) > 1 and self._bag[-1] == self._last:
                    self._bag[0], self._bag[-1] = self._bag[-1], self._bag[0]
            self._last = self._bag.pop()
            return self._last

    def is_portrait(self, path):
        """Orientation as displayed (EXIF rotation applied), or None if unreadable.
        Reads only the file header; results are cached."""
        if path not in self._portrait:
            try:
                with Image.open(path) as im:
                    w, h = im.size
                    if im.getexif().get(TAG_ORIENTATION) in (5, 6, 7, 8):  # rotated 90/270
                        w, h = h, w
            except Exception as e:
                log.debug("Cannot probe %s: %s", path, e)
                return None
            self._portrait[path] = h > w
        return self._portrait[path]

    def take_partner(self, exclude, portrait):
        """Remove and return one of the next PAIR_WINDOW photos in the shuffle that has
        the given orientation, or None. Reads file headers, so call it off the UI thread."""
        with self._lock:
            candidates = [p for p in reversed(self._bag[-PAIR_WINDOW:]) if p != exclude]
        for path in candidates:
            if self.is_portrait(path) == portrait:
                with self._lock:
                    if path in self._bag:
                        self._bag.remove(path)
                        return path
        return None


# --------------------------------------------------------------------------- EXIF metadata

def _exif_text(value):
    if isinstance(value, bytes):
        value = value.decode(errors="ignore")
    return str(value).strip("\x00 ") if value is not None else ""


def _gps_degrees(value, ref):
    try:
        d, m, s = (float(x) for x in value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    deg = d + m / 60 + s / 3600
    if not math.isfinite(deg):
        return None
    return -deg if _exif_text(ref).upper() in ("S", "W") else deg


def read_exif_meta(exif):
    """(date taken, (lat, lon)) from a Pillow Exif object; either may be None."""
    taken = None
    try:
        sub = exif.get_ifd(IFD_EXIF)
    except Exception:
        sub = {}
    for raw in (sub.get(TAG_DATETIME_ORIGINAL), exif.get(TAG_DATETIME)):
        try:
            taken = dt.datetime.strptime(_exif_text(raw)[:19], "%Y:%m:%d %H:%M:%S")
            break
        except ValueError:
            continue

    gps = None
    try:
        g = exif.get_ifd(IFD_GPS)
        lat, lon = _gps_degrees(g.get(2), g.get(1)), _gps_degrees(g.get(4), g.get(3))
        if (lat is not None and lon is not None and abs(lat) <= 90 and abs(lon) <= 180
                and (lat, lon) != (0.0, 0.0)):
            gps = (lat, lon)
    except Exception:
        pass
    return taken, gps


def format_coords(lat, lon):
    return f"{abs(lat):.4f}°{'N' if lat >= 0 else 'S'} {abs(lon):.4f}°{'E' if lon >= 0 else 'W'}"


class Geocoder:
    """Turns GPS coordinates into "Town, Country" using OpenStreetMap Nominatim.
    Results are cached on disk, and requests respect Nominatim's 1/second limit."""

    URL = "https://nominatim.openstreetmap.org/reverse"

    def __init__(self, language):
        self.language = language
        self.path = os.path.expanduser("~/.cache/picframe/geocode.json")
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._failed = set()
        try:
            with open(self.path, encoding="utf-8") as f:
                self._cache = json.load(f)
        except (OSError, ValueError):
            self._cache = {}

    def lookup(self, lat, lon):
        # Rounded to ~100 m: nearby photos share one lookup, and that's all we send.
        lat, lon = round(lat, 3), round(lon, 3)
        key = f"{lat:.3f},{lon:.3f}"
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            if key in self._failed:
                return None
            wait = self._last_request + 1.1 - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            try:
                place = self._fetch(lat, lon)
            except Exception as e:
                log.warning("Reverse geocoding %s failed: %s", key, e)
                self._failed.add(key)  # don't retry this session
                return None
            self._cache[key] = place
            self._save()
            return place

    def _fetch(self, lat, lon):
        query = urllib.parse.urlencode({"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 10,
                                        "accept-language": self.language})
        request = urllib.request.Request(
            f"{self.URL}?{query}",
            headers={"User-Agent": f"NasPicFrame/{VERSION} (personal photo frame)"})
        with urllib.request.urlopen(request, timeout=10) as r:
            address = json.load(r).get("address", {})
        town = next((address[k] for k in ("city", "town", "village", "municipality", "county", "state")
                     if address.get(k)), None)
        return ", ".join(p for p in (town, address.get("country")) if p) or None

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("Cannot save geocode cache: %s", e)


def caption_for(photo, settings, geocoder):
    parts = []
    if photo.taken:
        parts.append(photo.taken.strftime(settings.date_format))
    if photo.gps:
        place = geocoder.lookup(*photo.gps) if geocoder else None
        parts.append(place or format_coords(*photo.gps))
    return "  ·  ".join(parts)


# --------------------------------------------------------------------------- slide rendering
# Runs on loader threads; everything here is Pillow, nothing touches pygame.

def open_photo(path, draft_side):
    """Decode a photo, read its EXIF date/GPS, and turn it upright."""
    with Image.open(path) as im:
        taken, gps = read_exif_meta(im.getexif())
        im.draft("RGB", (draft_side, draft_side))  # JPEG: decode at reduced size - far faster
        image = ImageOps.exif_transpose(im).convert("RGB")
    return Photo(path, image, taken, gps)


def make_mat(img, size, blur, brightness):
    """A heavily blurred, darkened copy of the photo, cropped to fill `size`."""
    w, h = size
    # Blur at 1/10 scale and enlarge: looks the same, ~100x less work on a Pi.
    mat = ImageOps.fit(img, (max(1, w // 10), max(1, h // 10)), FAST)
    if blur:
        mat = mat.filter(ImageFilter.GaussianBlur(blur / 10))
    mat = ImageEnhance.Brightness(mat).enhance(brightness)
    return mat.resize(size, SMOOTH)


def place_photo(canvas, img, rect, settings):
    """Draw `img` into `rect` of the canvas (fitted, matted). Returns the photo's rect."""
    x, y, w, h = rect
    if settings.fit == "cover":
        canvas.paste(ImageOps.fit(img, (w, h), RESAMPLE), (x, y))
        return rect
    fitted = ImageOps.contain(img, (w, h), RESAMPLE)
    fw, fh = fitted.size
    if settings.matting == "blur" and (w - fw > 2 or h - fh > 2):
        canvas.paste(make_mat(img, (w, h), settings.mat_blur, settings.mat_brightness), (x, y))
    px, py = x + (w - fw) // 2, y + (h - fh) // 2
    canvas.paste(fitted, (px, py))
    return (px, py, fw, fh)


def panel_rects(size, count, gap):
    """One full-screen panel, or two side by side (landscape screen) / stacked (portrait)."""
    w, h = size
    if count == 1:
        return [(0, 0, w, h)]
    if w >= h:
        half = (w - gap) // 2
        return [(0, 0, half, h), (w - half, 0, half, h)]
    half = (h - gap) // 2
    return [(0, 0, w, half), (0, h - half, w, half)]


def render_slide(paths, size, settings, geocoder):
    photos = []
    for path in paths:
        try:
            photos.append(open_photo(path, max(size)))
        except Exception as e:  # corrupt file, vanished share, decompression bomb...
            log.warning("Skipping %s: %s", path, e)
    if not photos:
        raise RuntimeError("no readable photo")

    canvas = Image.new("RGB", size, settings.background)
    panels = []
    for photo, rect in zip(photos, panel_rects(size, len(photos), settings.pair_gap), strict=True):
        panels.append(Panel(place_photo(canvas, photo.image, rect, settings),
                            caption_for(photo, settings, geocoder)))
    return Slide(tuple(p.path for p in photos), canvas, panels)


class SlideLoader:
    """Builds one slide on a daemon thread, so a slow or hung NAS read never freezes
    the UI, the schedule or MQTT. Pass `paths` to rebuild a slide from history;
    otherwise the next photo(s) are taken from the shuffle."""

    def __init__(self, size, settings, library, geocoder, paths=None, index=None):
        self.paths = paths
        self.index = index
        self.slide = None
        self.error = None
        self.empty = False
        self.started = time.monotonic()
        self._done = threading.Event()
        threading.Thread(target=self._run, args=(size, settings, library, geocoder),
                         daemon=True).start()

    def _run(self, size, settings, library, geocoder):
        try:
            if self.paths is None:
                self.paths = self._choose(size, settings, library)
            if self.paths is None:
                self.empty = True
            else:
                self.slide = render_slide(self.paths, size, settings, geocoder)
        except Exception as e:
            self.error = e
        finally:
            self._done.set()

    @staticmethod
    def _choose(size, settings, library):
        first = library.next()
        if first is None:
            return None
        if settings.pair_portraits:
            # Pair photos whose orientation is "wrong" for the screen: portraits on a
            # landscape screen, landscapes on a portrait (rotated) screen.
            screen_portrait = size[1] > size[0]
            portrait = library.is_portrait(first)
            if portrait is not None and portrait != screen_portrait:
                partner = library.take_partner(first, portrait)
                if partner:
                    return (first, partner)
        return (first,)

    @property
    def done(self):
        return self._done.is_set()


# --------------------------------------------------------------------------- screen

def load_font(name, size):
    if name and os.path.isfile(name):
        return pygame.font.Font(name, size)
    return pygame.font.SysFont(name or "dejavusans,notosans,freesans,liberationsans", size)


def builtin_placeholder(size):
    """The built-in "no photos yet" image: a dusk gradient with a soft photo icon,
    drawn in code so it is sharp at any resolution. Returns (image, icon bottom y)."""
    w, h = size
    # Vertical gradient: a 1-pixel strip, stretched.
    top, bottom = (40, 54, 78), (12, 14, 20)
    strip = Image.new("RGB", (1, 256))
    strip.putdata([tuple(int(t + (b - t) * i / 255) for t, b in zip(top, bottom, strict=True))
                   for i in range(256)])
    canvas = strip.resize(size, SMOOTH)

    # Photo icon (frame, sun, two hills), drawn at 4x and scaled down for smooth edges.
    k = 4
    ih = max(32, h // 5)
    iw = ih * 4 // 3
    W, H = iw * k, ih * k
    line = max(2, ih // 22) * k
    mask = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([line // 2, line // 2, W - line // 2 - 1, H - line // 2 - 1],
                        radius=ih // 7 * k, outline=255, width=line)
    sun_x, sun_y, sun_r = W * 0.70, H * 0.32, H * 0.09
    d.ellipse([sun_x - sun_r, sun_y - sun_r, sun_x + sun_r, sun_y + sun_r], fill=255)
    base = H - line * 2
    d.polygon([(line * 2, base), (W * 0.36, H * 0.42), (W * 0.62, base)], fill=255)
    d.polygon([(W * 0.44, base), (W * 0.68, H * 0.58), (W - line * 2, base)], fill=255)
    mask = mask.resize((iw, ih), RESAMPLE).point(lambda v: v * 55 // 100)  # translucent

    x, y = (w - iw) // 2, (h - ih) // 2 - ih // 4
    canvas.paste(Image.new("RGB", (iw, ih), (175, 190, 210)), (x, y), mask)
    return canvas, y + ih


class Screen:
    def __init__(self, settings):
        self.settings = settings
        self.background = pygame.Color(settings.background)
        self.show_clock = settings.show_clock
        self.show_info = settings.show_info
        self.clock_text = None
        self.open()

    def open(self):
        pygame.display.init()
        pygame.display.set_caption("NasPicFrame")
        self.surface = pygame.display.set_mode((0, 0), pygame.FULLSCREEN | pygame.NOFRAME)
        self.size = self.surface.get_size()
        pygame.mouse.set_visible(False)
        try:  # belt and braces: a fully transparent cursor for compositors that ignore the above
            pygame.mouse.set_cursor((8, 8), (0, 0), (0,) * 8, (0,) * 8)
        except pygame.error:
            pass
        h = self.size[1]
        self.caption_font = load_font(self.settings.font, max(14, h // 40))
        self.clock_font = load_font(self.settings.font, max(24, h // 14))
        self.margin = max(8, h // 50)
        log.info("Screen %dx%d (%s)", *self.size, pygame.display.get_driver())
        self.placeholder, self.placeholder_anchor = self._build_placeholder()
        self.blank()

    def _build_placeholder(self):
        """Shown while there are no photos: `placeholder_image` if set, else built in.
        Returns the surface and where the status text goes."""
        w, h = self.size
        path = self.settings.placeholder_image
        if path:
            try:
                canvas = Image.new("RGB", self.size, self.settings.background)
                place_photo(canvas, open_photo(path, max(self.size)).image,
                            (0, 0, w, h), self.settings)
                return self._to_surface(canvas), {"midbottom": (w // 2, h - self.margin * 2)}
            except Exception as e:
                log.warning("Cannot use placeholder_image %s: %s - using the built-in one",
                            path, e)
        canvas, icon_bottom = builtin_placeholder(self.size)
        return self._to_surface(canvas), {"midtop": (w // 2, icon_bottom + self.margin * 2)}

    @staticmethod
    def _to_surface(pil_image):
        return _frombytes(pil_image.tobytes(), pil_image.size, "RGB").convert()

    def reopen(self):
        pygame.display.quit()
        self.open()

    def blank(self):
        self.frame, self.panels, self.message_text, self.clock_text = None, [], None, None
        self.surface.fill(self.background)
        pygame.display.flip()

    def message(self, text):
        self.frame, self.panels, self.message_text = None, [], text
        self.redraw()

    def show_slide(self, slide):
        frame = self._to_surface(slide.image)
        previous = self.frame
        self.frame, self.panels, self.message_text = frame, slide.panels, None

        ms = self.settings.transition_ms
        if previous is not None and ms > 0:
            clock = pygame.time.Clock()
            start = time.monotonic()
            while (t := (time.monotonic() - start) * 1000 / ms) < 1:
                frame.set_alpha(int(255 * t))
                self.surface.blit(previous, (0, 0))
                self.surface.blit(frame, (0, 0))
                if self.show_clock:
                    self._draw_clock()
                pygame.display.flip()
                pygame.event.pump()
                clock.tick(30)
            frame.set_alpha(None)
        self.redraw()

    def redraw(self):
        if self.message_text is not None:
            self.surface.blit(self.placeholder, (0, 0))
            self._draw_text(self.caption_font, self.message_text, **self.placeholder_anchor)
        elif self.frame is not None:
            self.surface.blit(self.frame, (0, 0))
            if self.show_info:
                for panel in self.panels:
                    if panel.caption:
                        x, y, w, h = panel.rect
                        self._draw_text(self.caption_font, panel.caption,
                                        bottomright=(x + w - self.margin, y + h - self.margin))
        else:
            self.surface.fill(self.background)
        if self.show_clock:
            self._draw_clock()
        pygame.display.flip()

    def _draw_clock(self):
        self.clock_text = time.strftime(self.settings.clock_format)
        self._draw_text(self.clock_font, self.clock_text,
                        bottomleft=(self.margin, self.size[1] - self.margin))

    def _draw_text(self, font, text, **anchor):
        """Subtle, slightly translucent text with a soft shadow, legible on any photo."""
        fg = font.render(text, True, (240, 240, 240))
        shadow = font.render(text, True, (0, 0, 0))
        fg.set_alpha(215)
        shadow.set_alpha(150)
        rect = fg.get_rect(**anchor)
        offset = max(1, font.get_height() // 18)
        self.surface.blit(shadow, rect.move(offset, offset))
        self.surface.blit(fg, rect)


# --------------------------------------------------------------------------- MQTT / Home Assistant

class MqttBridge:
    """MQTT control with Home Assistant discovery. Commands go onto a queue that the
    main loop drains; state is published retained and re-sent whenever the broker
    or Home Assistant restarts."""

    SWITCHES = {
        "display": ("Display", "mdi:monitor"),
        "pause": ("Pause slideshow", "mdi:pause"),
        "clock": ("Clock", "mdi:clock-outline"),
        "info": ("Photo info", "mdi:information-outline"),
    }
    BUTTONS = {
        "next": ("Next photo", "mdi:skip-next"),
        "previous": ("Previous photo", "mdi:skip-previous"),
    }

    def __init__(self, settings, commands):
        import paho.mqtt.client as mqtt

        self.commands = commands
        self.node = settings.mqtt_node_id
        self.base = f"{settings.mqtt_topic_prefix}/{self.node}"
        self.discovery = settings.mqtt_discovery_prefix
        self.device_name = settings.mqtt_device_name
        self.availability = f"{self.base}/availability"
        self._state = {}
        self._lock = threading.Lock()

        client_id = f"picframe-{self.node}"
        try:
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        except AttributeError:  # paho-mqtt 1.x (Debian Bookworm)
            self.client = mqtt.Client(client_id=client_id)
        if settings.mqtt_username:
            self.client.username_pw_set(settings.mqtt_username, settings.mqtt_password or None)
        self.client.will_set(self.availability, "offline", qos=1, retain=True)
        self.client.reconnect_delay_set(1, 60)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        # Asynchronous: keeps retrying in the background if the broker isn't up yet.
        self.client.connect_async(settings.mqtt_host, settings.mqtt_port, keepalive=60)
        self.client.loop_start()
        log.info("MQTT: connecting to %s:%d as %s", settings.mqtt_host, settings.mqtt_port, self.base)

    # Signature works for both paho 1.x (rc int) and 2.x (ReasonCode, properties).
    def _on_connect(self, client, userdata, flags, rc, properties=None):
        failed = rc.is_failure if hasattr(rc, "is_failure") else rc != 0
        if failed:
            log.warning("MQTT connection refused: %s", rc)
            return
        log.info("MQTT connected")
        client.subscribe(f"{self.base}/+/set", qos=1)
        client.subscribe(f"{self.discovery}/status", qos=1)  # Home Assistant birth message
        self._announce()

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode(errors="ignore").strip()
        if msg.topic == f"{self.discovery}/status":
            if payload == "online":  # Home Assistant restarted: re-announce everything
                self._announce()
            return
        key = msg.topic.split("/")[-2]
        if key in self.SWITCHES and payload.upper() in ("ON", "OFF"):
            self.commands.put((key, payload.upper() == "ON"))
        elif key in self.BUTTONS:
            self.commands.put((key, None))

    def _announce(self):
        device = {
            "identifiers": [f"picframe_{self.node}"],
            "name": self.device_name,
            "manufacturer": "NasPicFrame",
            "model": "Raspberry Pi photo frame",
            "sw_version": VERSION,
        }
        common = {"availability_topic": self.availability, "device": device}
        for key, (name, icon) in self.SWITCHES.items():
            self._discover("switch", key, name=name, icon=icon,
                           command_topic=f"{self.base}/{key}/set",
                           state_topic=f"{self.base}/{key}/state", **common)
        for key, (name, icon) in self.BUTTONS.items():
            self._discover("button", key, name=name, icon=icon,
                           command_topic=f"{self.base}/{key}/set", **common)
        self._discover("sensor", "photo", name="Current photo", icon="mdi:image",
                       state_topic=f"{self.base}/photo/state",
                       json_attributes_topic=f"{self.base}/photo/attributes", **common)

        self.client.publish(self.availability, "online", qos=1, retain=True)
        with self._lock:
            states = list(self._state.items())
        for key, (payload, attributes) in states:
            self._send(key, payload, attributes)

    def _discover(self, component, key, **config):
        config["unique_id"] = f"picframe_{self.node}_{key}"
        self.client.publish(f"{self.discovery}/{component}/{self.node}/{key}/config",
                            json.dumps(config), qos=1, retain=True)

    def publish(self, key, value, attributes=None):
        payload = ("ON" if value else "OFF") if isinstance(value, bool) else str(value)[:255]
        with self._lock:
            self._state[key] = (payload, attributes)
        self._send(key, payload, attributes)

    def _send(self, key, payload, attributes):
        self.client.publish(f"{self.base}/{key}/state", payload, qos=1, retain=True)
        if attributes is not None:
            self.client.publish(f"{self.base}/{key}/attributes", json.dumps(attributes),
                                qos=1, retain=True)

    def close(self):
        try:
            self.client.publish(self.availability, "offline", qos=1, retain=True).wait_for_publish(2)
        except Exception:
            pass
        self.client.disconnect()
        self.client.loop_stop()


# --------------------------------------------------------------------------- main loop

class PictureFrame:
    def __init__(self, settings):
        self.s = settings
        fix_session_env()
        self.power = DisplayPower(settings.display_method, settings.display_output,
                                  settings.cec_device)
        self.power.set(True)  # we may have been restarted while the screen was off
        pygame.font.init()
        self.screen = Screen(settings)
        self.library = ImageLibrary(settings)
        self.geocoder = Geocoder(settings.geocode_language) if settings.geocode == "nominatim" else None
        self.commands = queue.Queue()
        self.mqtt = self._start_mqtt()
        self.stop = threading.Event()
        self.clock = pygame.time.Clock()

        self.display_on = None   # actual display state (None = not decided yet)
        self.scheduled = None    # last schedule state; a change overrides manual on/off
        self.paused = False
        self.history = []        # shown slides, as tuples of paths
        self.pos = -1            # index of the slide on screen
        self.goto = 0            # pending navigation: +1 next, -1 previous, 0 none
        self.upcoming = None     # SlideLoader preparing the next new slide
        self.requested = None    # SlideLoader rebuilding a slide from history
        self.next_change = 0.0   # when the slideshow advances on its own
        self.hold_until = 0.0    # back-off before loading again (no photos / NAS down)
        self.failures = 0
        self.config_mtime = file_mtime(settings.path)
        self.last_config_check = 0.0

    def _start_mqtt(self):
        if not self.s.mqtt_enabled:
            return None
        try:
            return MqttBridge(self.s, self.commands)
        except ImportError:
            log.error("MQTT enabled but paho-mqtt is missing: sudo apt install python3-paho-mqtt")
            return None

    def _publish(self, key, value, attributes=None):
        if self.mqtt:
            self.mqtt.publish(key, value, attributes)

    def run(self):
        signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
        signal.signal(signal.SIGINT, lambda *_: self.stop.set())
        self._publish("pause", self.paused)
        self._publish("clock", self.screen.show_clock)
        self._publish("info", self.screen.show_info)
        try:
            while not self.stop.is_set():
                self._handle_keys()
                self._handle_commands()
                mono = time.monotonic()

                if mono - self.last_config_check > 5:
                    self.last_config_check = mono
                    if file_mtime(self.s.path) != self.config_mtime:
                        log.info("Config file changed - restarting")
                        return RESTART

                scheduled = is_awake(self.s.wake_time, self.s.sleep_time, dt.datetime.now().time())
                if scheduled != self.scheduled:
                    self.scheduled = scheduled
                    self.set_display(scheduled)

                if self.display_on:
                    self._advance(mono)
                    if self.screen.show_clock and time.strftime(self.s.clock_format) != self.screen.clock_text:
                        self.screen.redraw()
                self.clock.tick(10 if self.display_on else 2)
        finally:
            if self.mqtt:
                self.mqtt.close()
            pygame.quit()
            if self.display_on is False and self.stop.is_set():
                self.power.set(True)  # don't leave a dark screen behind when stopped by hand
        return None

    # ---- commands (MQTT, keyboard)

    def _handle_keys(self):
        keys = {pygame.K_RIGHT: ("next", None), pygame.K_LEFT: ("previous", None)}
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.stop.set()
            elif event.type == pygame.KEYDOWN:
                if event.key in (pygame.K_ESCAPE, pygame.K_q):
                    self.stop.set()
                elif event.key in keys:
                    self.command(*keys[event.key])
                elif event.key == pygame.K_SPACE:
                    self.command("pause", not self.paused)
                elif event.key == pygame.K_c:
                    self.command("clock", not self.screen.show_clock)
                elif event.key == pygame.K_i:
                    self.command("info", not self.screen.show_info)
                elif event.key == pygame.K_d:
                    self.command("display", not self.display_on)

    def _handle_commands(self):
        while True:
            try:
                key, value = self.commands.get_nowait()
            except queue.Empty:
                return
            self.command(key, value)

    def command(self, key, value=None):
        log.info("Command: %s%s", key, "" if value is None else f" {'on' if value else 'off'}")
        if key == "display":
            self.set_display(bool(value))  # lasts until the next scheduled switch
        elif key == "pause":
            self.paused = bool(value)
            self.next_change = time.monotonic() + self.s.delay
            self._publish("pause", self.paused)
        elif key in ("clock", "info"):
            setattr(self.screen, f"show_{key}", bool(value))
            if self.display_on:
                self.screen.redraw()
            self._publish(key, bool(value))
        elif key == "next" and self.display_on:
            self.goto = 1
        elif key == "previous" and self.display_on:
            self.goto = -1

    def set_display(self, on):
        if on == self.display_on:
            return
        if on:
            self.power.set(True)
            if self.display_on is False and self.power.reopen_after_wake:
                self.screen.reopen()
            self.goto = 1  # a fresh photo straight away
        else:
            self.screen.blank()
            self.power.set(False)
            self.goto, self.requested = 0, None
        self.display_on = on
        self._publish("display", on)

    # ---- slideshow

    def _advance(self, mono):
        if self.goto == 0 and not self.paused and mono >= self.next_change:
            self.goto = 1
        # Always keep the next new slide loading in the background.
        if self.upcoming is None and mono >= self.hold_until:
            self.upcoming = SlideLoader(self.screen.size, self.s, self.library, self.geocoder)

        if self.goto == 1 and self.pos >= len(self.history) - 1:
            self._take_upcoming(mono)
        elif self.goto != 0:
            self._take_from_history(mono)

    def _take_upcoming(self, mono):
        loader = self.upcoming
        if loader is None:
            return
        if not loader.done:
            if mono - loader.started > LOAD_TIMEOUT:
                log.warning("Gave up loading %s after %ds", loader.paths, LOAD_TIMEOUT)
                self.upcoming = None
            return
        self.upcoming = None

        if loader.empty:
            self.screen.message(f"Waiting for photos in {', '.join(self.s.image_dirs)} ...")
            self.goto = 0
            self.next_change = self.hold_until = mono + 10
            return
        if loader.slide is None:
            log.warning("Skipping %s: %s", loader.paths, loader.error)
            self.failures += 1
            if self.failures >= MAX_FAILURES:
                log.warning("%d unreadable photos in a row - is the share up? Backing off",
                            self.failures)
                self.failures = 0
                self.goto = 0
                self.next_change = self.hold_until = mono + 30
            return  # otherwise goto stays 1: try the next photo on the next tick

        self.failures = 0
        self.history.append(loader.slide.paths)
        if len(self.history) > HISTORY_SIZE:
            del self.history[0]
            self.requested = None  # its index is now stale
        self.pos = len(self.history) - 1
        self._show(loader.slide)

    def _take_from_history(self, mono):
        index = self.pos + self.goto
        if not 0 <= index < len(self.history):
            self.goto = 0
            return
        if self.requested is None or self.requested.index != index:
            self.requested = SlideLoader(self.screen.size, self.s, self.library, self.geocoder,
                                         paths=self.history[index], index=index)
        loader = self.requested
        if not loader.done:
            if mono - loader.started > LOAD_TIMEOUT:
                log.warning("Gave up loading %s after %ds", loader.paths, LOAD_TIMEOUT)
                self.requested, self.goto = None, 0
            return
        self.requested = None
        if loader.slide is None:
            log.warning("Cannot show %s again: %s", loader.paths, loader.error)
            self.goto = 0
            return
        self.pos = index
        self._show(loader.slide)

    def _show(self, slide):
        self.screen.show_slide(slide)
        self.goto = 0
        self.next_change = time.monotonic() + self.s.delay
        log.debug("Showing %s", ", ".join(slide.paths))
        self._publish("photo", " + ".join(os.path.basename(p) for p in slide.paths), {
            "paths": list(slide.paths),
            "captions": [p.caption for p in slide.panels],
        })


def main():
    ap = argparse.ArgumentParser(description="NasPicFrame slideshow")
    ap.add_argument("-c", "--config", default=DEFAULT_CONFIG, help="path to config.ini")
    ap.add_argument("--wait", action="store_true",
                    help="wait for the display session and photo directory, then exit")
    ap.add_argument("--wait-timeout", type=int, default=120, metavar="SECONDS")
    ap.add_argument("--display", choices=("on", "off"), help="switch the display and exit")
    ap.add_argument("--migrate-config", metavar="TEMPLATE",
                    help="add options from TEMPLATE that are missing in the config, then exit")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = ap.parse_args()

    # No timestamps: journald adds its own.
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    if args.migrate_config:
        return migrate_config(args.config, args.migrate_config)
    settings = Settings(args.config)

    if args.wait:
        return wait_until_ready(settings, args.wait_timeout)
    if args.display:
        fix_session_env()
        DisplayPower(settings.display_method, settings.display_output,
                     settings.cec_device).set(args.display == "on")
        return 0

    if PictureFrame(settings).run() == RESTART:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
