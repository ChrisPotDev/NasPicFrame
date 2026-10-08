#!/usr/bin/env python3
"""NasPicFrame music add-on coordinator.

One small service that is the single source of truth for "who is playing":
  - starts/stops the receivers enabled in music.ini (YouTube, Spotify, AirPlay);
  - routes audio to the USB DAC, the headphone jack or HDMI, following hot-plug;
  - enforces the volume cap, including quiet hours;
  - "last source wins": when a second source starts, the first is stopped
    (or the newcomer is, while locked from Home Assistant);
  - writes what's playing for the picture frame and publishes it to Home Assistant.

Audio state comes from PipeWire through its PulseAudio interface (`pactl`): every
receiver plays under a fixed application name (picframe-youtube/-spotify/-airplay).
"""

import argparse
import base64
import datetime as dt
import hashlib
import json
import logging
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from musicconf import DEFAULT_CONFIG, SOURCES, Settings  # noqa: E402

VERSION = "1.0"
log = logging.getLogger("music")

UNITS = {s: f"music-{s}.service" for s in SOURCES}
APP_NAMES = {f"picframe-{s}": s for s in SOURCES}
# Fallback when a stream doesn't carry our application name.
BINARIES = {"librespot": "spotify", "shairport-sync": "airplay"}
YOUTUBE_CONTROL = "http://127.0.0.1:8766"

STOP_RETRY_SECONDS = 10       # don't re-send a stop to the same source more often
NOW_PLAYING_GRACE = 5         # keep the card through short gaps between tracks
NOW_PLAYING_REFRESH = 10      # rewrite while playing so the frame knows it's fresh
HINT_REFRESH = 60


def runtime_dirs():
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    music, frame = os.path.join(base, "picframe-music"), os.path.join(base, "picframe")
    os.makedirs(music, exist_ok=True)
    os.makedirs(frame, exist_ok=True)
    return music, frame


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


# --------------------------------------------------------------------------- audio (pactl)

def pactl_json(*args):
    try:
        r = subprocess.run(["pactl", "-f", "json", *args], capture_output=True, text=True,
                           timeout=5, check=False)
        return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else []
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        log.debug("pactl %s failed: %s", " ".join(args), e)
        return []


def pactl(*args):
    try:
        r = subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=5, check=False)
        return r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("pactl %s failed: %s", " ".join(args), e)
        return ""


def sink_kind(sink):
    """'usb', 'headphones', 'hdmi' or None for one `pactl list sinks` entry."""
    props = sink.get("properties", {})
    text = " ".join(str(x) for x in (
        sink.get("name", ""), sink.get("description", ""),
        props.get("alsa.card_name", ""), props.get("device.product.name", ""))).lower()
    if props.get("device.bus") == "usb" or "usb" in text:
        return "usb"
    if "hdmi" in text:
        return "hdmi"
    if "headphones" in text or "bcm2835" in text:
        return "headphones"
    return None


def choose_sink(sinks, output, usb_device=""):
    """Name of the sink to use for the `output` setting, or None if unavailable."""
    def of_kind(kind):
        found = [s for s in sinks if sink_kind(s) == kind]
        if kind == "usb" and usb_device:
            wanted = usb_device.lower()
            found = [s for s in found if wanted in (s.get("name", "") + " " +
                                                    s.get("description", "")).lower()]
        return found[0]["name"] if found else None

    if output == "auto":
        return of_kind("usb") or of_kind("headphones")
    return of_kind(output)


def sink_volume_percent(sink):
    levels = []
    for channel in (sink.get("volume") or {}).values():
        m = re.match(r"(\d+)%", str(channel.get("value_percent", "")))
        if m:
            levels.append(int(m.group(1)))
    return max(levels) if levels else None


def stream_source(stream):
    props = stream.get("properties", {})
    return (APP_NAMES.get(props.get("application.name", ""))
            or BINARIES.get(props.get("application.process.binary", "")))


def playing_sources(sink_inputs):
    """Sources with an active (uncorked) stream."""
    return {src for s in sink_inputs if not s.get("corked") and (src := stream_source(s))}


# --------------------------------------------------------------------------- policy

def decide(playing_since, active, locked, allow_takeover):
    """Pick which source keeps playing.

    playing_since: {source: time it started playing} for everything playing now.
    Returns (source to keep or None, [sources to stop])."""
    if not playing_since:
        return None, []
    if len(playing_since) == 1:
        (only,) = playing_since
        return only, []
    newest = max(playing_since, key=playing_since.get)
    if locked or not allow_takeover:
        # Keep whatever was playing; the newcomer is turned away.
        keep = active if active in playing_since else min(playing_since, key=playing_since.get)
    else:
        keep = newest
    return keep, sorted(s for s in playing_since if s != keep)


def now_playing_from(source, state, artwork_path):
    """The record the picture frame reads, or None."""
    if not source or not state:
        return None
    return {
        "state": state.get("state", "playing"),
        "source": source,
        "title": state.get("title", ""),
        "artist": state.get("artist", ""),
        "album": state.get("album", ""),
        "artwork": artwork_path or "",
        "updated": time.time(),
    }


# --------------------------------------------------------------------------- AirPlay metadata

_ITEM_RE = re.compile(
    r"<item><type>([0-9a-fA-F]{8})</type><code>([0-9a-fA-F]{8})</code>"
    r"<length>(\d+)</length>(?:\s*<data encoding=\"base64\">\s*([^<]*?)\s*</data>)?\s*</item>",
    re.S)


def parse_metadata_items(buffer):
    """Parse shairport-sync's metadata pipe format. Returns ([(type, code, bytes)], rest)."""
    items, end = [], 0
    for m in _ITEM_RE.finditer(buffer):
        kind, code = (bytes.fromhex(x).decode("ascii", "replace") for x in m.group(1, 2))
        try:
            data = base64.b64decode(m.group(4)) if m.group(4) else b""
        except ValueError:
            data = b""
        items.append((kind, code, data))
        end = m.end()
    return items, buffer[end:]


class AirPlayMetadata(threading.Thread):
    """Reads shairport-sync's metadata pipe and keeps airplay.json up to date."""

    def __init__(self, runtime):
        super().__init__(name="airplay-metadata", daemon=True)
        self.pipe = os.path.join(runtime, "shairport-metadata")
        self.state_path = os.path.join(runtime, "airplay.json")
        self.art_path = os.path.join(runtime, "airplay-art")
        self.state = {}

    def run(self):
        while True:
            try:
                if not os.path.exists(self.pipe):
                    os.mkfifo(self.pipe, 0o600)
                with open(self.pipe, encoding="ascii", errors="replace") as f:  # blocks until a writer
                    buffer = ""
                    for chunk in iter(lambda: f.read(4096), ""):
                        items, buffer = parse_metadata_items(buffer + chunk)
                        for item in items:
                            self.apply(*item)
                        buffer = buffer[-2_000_000:]  # cover art can be large; never unbounded
            except OSError as e:
                log.debug("AirPlay metadata pipe: %s", e)
            time.sleep(2)

    def apply(self, kind, code, data):
        text = data.decode("utf-8", "replace")
        if kind == "core" and code in ("minm", "asar", "asal"):
            self.state[{"minm": "title", "asar": "artist", "asal": "album"}[code]] = text
        elif kind == "ssnc" and code == "PICT" and data:
            ext = ".png" if data[:4] == b"\x89PNG" else ".jpg"
            for old in (".png", ".jpg"):
                remove(self.art_path + old)
            with open(self.art_path + ext, "wb") as f:
                f.write(data)
            self.state["artwork_file"] = self.art_path + ext
        elif kind == "ssnc" and code in ("pbeg", "prsm"):
            self.state["state"] = "playing"
        elif kind == "ssnc" and code == "paus":
            self.state["state"] = "paused"
        elif kind == "ssnc" and code == "pend":
            self.state = {}
            remove(self.state_path)
            return
        else:
            return
        self.state["updated"] = time.time()
        write_json(self.state_path, self.state)


# --------------------------------------------------------------------------- artwork

class Artwork:
    """Downloads remote artwork (YouTube thumbnails, Spotify covers) in the background."""

    def __init__(self, folder):
        self.folder = folder
        self.url = None
        self.path = None
        self._lock = threading.Lock()

    def get(self, url):
        """Local path for `url` once downloaded, else None (download starts)."""
        with self._lock:
            if url == self.url:
                return self.path
            self.url, self.path = url, None
        threading.Thread(target=self._fetch, args=(url,), daemon=True).start()
        return None

    def _fetch(self, url):
        path = os.path.join(self.folder, "artwork-" + hashlib.sha1(url.encode()).hexdigest()[:12])
        try:
            req = urllib.request.Request(url, headers={"User-Agent": f"NasPicFrame-music/{VERSION}"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = r.read(5_000_000)
            with open(path, "wb") as f:
                f.write(data)
        except OSError as e:
            log.info("Artwork download failed: %s", e)
            return
        with self._lock:
            if self.url == url:
                old, self.path = self.path, path
                if old and old != path:
                    remove(old)


# --------------------------------------------------------------------------- Home Assistant

class MqttBridge:
    """Home Assistant entities over MQTT discovery. Commands go onto a queue."""

    def __init__(self, settings, commands):
        import paho.mqtt.client as mqtt

        self.commands = commands
        self.node = f"{settings.mqtt_node_id}_music"
        self.base = f"{settings.mqtt_topic_prefix}/{self.node}"
        self.discovery = settings.mqtt_discovery_prefix
        self.availability = f"{self.base}/availability"
        self.device = {
            "identifiers": [f"picframe_{self.node}"],
            "name": f"{settings.name} Speaker",
            "manufacturer": "NasPicFrame",
            "model": "Music add-on",
            "sw_version": VERSION,
        }
        self.max_volume = settings.max_volume
        self._state = {}
        self._lock = threading.Lock()

        client_id = f"picframe-{self.node}"
        try:
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        except AttributeError:  # paho-mqtt 1.x
            self.client = mqtt.Client(client_id=client_id)
        if settings.mqtt_username:
            self.client.username_pw_set(settings.mqtt_username, settings.mqtt_password or None)
        self.client.will_set(self.availability, "offline", qos=1, retain=True)
        self.client.reconnect_delay_set(1, 60)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.connect_async(settings.mqtt_host, settings.mqtt_port, keepalive=60)
        self.client.loop_start()

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc.is_failure if hasattr(rc, "is_failure") else rc != 0:
            log.warning("MQTT connection refused: %s", rc)
            return
        client.subscribe(f"{self.base}/+/set", qos=1)
        client.subscribe(f"{self.discovery}/status", qos=1)
        self._announce()

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode(errors="ignore").strip()
        if msg.topic == f"{self.discovery}/status":
            if payload == "online":
                self._announce()
            return
        key = msg.topic.split("/")[-2]
        if key == "stop":
            self.commands.put(("stop", None))
        elif key == "lock" and payload.upper() in ("ON", "OFF"):
            self.commands.put(("lock", payload.upper() == "ON"))
        elif key == "volume":
            try:
                self.commands.put(("volume", int(float(payload))))
            except ValueError:
                pass

    def _announce(self):
        common = {"availability_topic": self.availability, "device": self.device}
        b = self.base
        self._discover("sensor", "now_playing", name="Now playing", icon="mdi:music",
                       state_topic=f"{b}/now_playing/state",
                       json_attributes_topic=f"{b}/now_playing/attributes", **common)
        self._discover("sensor", "source", name="Source", icon="mdi:cast-audio",
                       state_topic=f"{b}/source/state", **common)
        self._discover("number", "volume", name="Volume", icon="mdi:volume-high",
                       command_topic=f"{b}/volume/set", state_topic=f"{b}/volume/state",
                       min=0, max=self.max_volume, step=1, unit_of_measurement="%", **common)
        self._discover("button", "stop", name="Stop", icon="mdi:stop",
                       command_topic=f"{b}/stop/set", **common)
        self._discover("switch", "lock", name="Lock", icon="mdi:lock",
                       command_topic=f"{b}/lock/set", state_topic=f"{b}/lock/state", **common)
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
            if self._state.get(key) == (payload, attributes):
                return
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
        except Exception:  # noqa: BLE001 - best effort on shutdown
            pass
        self.client.disconnect()
        self.client.loop_stop()


# --------------------------------------------------------------------------- coordinator

def systemctl(*args):
    try:
        subprocess.run(["systemctl", "--user", *args], capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("systemctl --user %s failed: %s", " ".join(args), e)


class Coordinator:
    def __init__(self, settings):
        self.s = settings
        self.runtime, self.frame_runtime = runtime_dirs()
        self.now_playing_path = os.path.join(self.frame_runtime, "nowplaying.json")
        self.hint_path = os.path.join(self.frame_runtime, "music-hint.json")
        self.commands = queue.Queue()
        self.mqtt = None
        if settings.mqtt_enabled:
            try:
                self.mqtt = MqttBridge(settings, self.commands)
            except ImportError:
                log.error("MQTT enabled but paho-mqtt is missing: sudo apt install python3-paho-mqtt")
        self.artwork = Artwork(self.runtime)
        self.active = None
        self.playing_since = {}
        self.stop_sent = {}
        self.locked = False
        self.ha_volume = None       # Home Assistant's level; None = use the cap
        self.last_playing = 0.0
        self.last_np_write = 0.0
        self.last_hint_write = 0.0
        self.config_mtime = os.stat(settings.path).st_mtime

    def start_receivers(self):
        for source in SOURCES:
            if self.s.enabled[source]:
                systemctl("start", UNITS[source])
                log.info("%s receiver on, as \"%s\"", source, self.s.names[source])
            else:
                systemctl("stop", UNITS[source])
        if self.s.enabled["airplay"]:
            AirPlayMetadata(self.runtime).start()

    def run(self):
        self.start_receivers()
        self._publish("lock", self.locked)
        while True:
            self._handle_commands()
            if self._config_changed():
                log.info("music.ini changed - restarting receivers")
                for source in SOURCES:
                    if self.s.enabled[source]:
                        systemctl("restart", UNITS[source])
                return "restart"
            self.tick()
            time.sleep(1)

    def _config_changed(self):
        try:
            return os.stat(self.s.path).st_mtime != self.config_mtime
        except OSError:
            return False

    def _handle_commands(self):
        while True:
            try:
                key, value = self.commands.get_nowait()
            except queue.Empty:
                return
            log.info("Home Assistant: %s %s", key, "" if value is None else value)
            if key == "stop" and self.active:
                self.stop_source(self.active, force=True)
            elif key == "lock":
                self.locked = bool(value)
                self._publish("lock", self.locked)
            elif key == "volume":
                self.ha_volume = max(0, min(100, value))

    def tick(self):
        sinks = pactl_json("list", "sinks")
        inputs = pactl_json("list", "sink-inputs")
        self.route(sinks, inputs)

        now = time.time()
        playing = playing_sources(inputs)
        self.playing_since = {s: self.playing_since.get(s, now) for s in playing}
        keep, stop = decide(self.playing_since, self.active, self.locked, self.s.allow_takeover)
        for source in stop:
            self.stop_source(source)
        if keep != self.active and keep:
            log.info("Now playing from %s", keep)
        if keep:
            self.active = keep
            self.last_playing = now
        elif self.active and now - self.last_playing > NOW_PLAYING_GRACE:
            self.active = None
        self.update_now_playing(now)
        self.update_hint(now)
        self._publish("source", self.active or "idle")

    def route(self, sinks, inputs):
        target = choose_sink(sinks, self.s.output, self.s.usb_device)
        if not target:
            return
        sink = next(s for s in sinks if s["name"] == target)
        if pactl("get-default-sink") != target:
            log.info("Audio output: %s", sink.get("description") or target)
            pactl("set-default-sink", target)
        for stream in inputs:
            if stream.get("sink") != sink.get("index") and stream_source(stream):
                pactl("move-sink-input", str(stream["index"]), target)

        cap = self.s.volume_cap(dt.datetime.now().time())
        level = cap if self.ha_volume is None else min(cap, self.ha_volume)
        current = sink_volume_percent(sink)
        if current is None or abs(current - level) > 1:
            pactl("set-sink-volume", target, f"{level}%")
            pactl("set-sink-mute", target, "0")
        self._publish("volume", level)

    def stop_source(self, source, force=False):
        now = time.time()
        if not force and now - self.stop_sent.get(source, 0) < STOP_RETRY_SECONDS:
            return
        self.stop_sent[source] = now
        log.info("Stopping %s (%s)", source,
                 "requested" if force else "locked" if self.locked else "another source took over")
        if source == "youtube":
            try:
                req = urllib.request.Request(f"{YOUTUBE_CONTROL}/pause", method="POST", data=b"")
                urllib.request.urlopen(req, timeout=5).close()
                return
            except OSError:
                pass  # fall through to restarting the receiver
        # librespot and shairport-sync have no "pause this guest" command: restarting
        # ends the session (the guest's app shows it disconnected).
        systemctl("restart", UNITS[source])

    def update_now_playing(self, now):
        state = read_json(os.path.join(self.runtime, f"{self.active}.json")) if self.active else None
        artwork = None
        if state:
            artwork = state.get("artwork_file") or (
                self.artwork.get(state["artwork_url"]) if state.get("artwork_url") else None)
        record = now_playing_from(self.active, state, artwork)
        if record is None:
            remove(self.now_playing_path)
            self._publish("now_playing", "Idle", {})
            return
        previous = read_json(self.now_playing_path) or {}
        changed = any(previous.get(k) != record[k] for k in ("state", "source", "title", "artist", "artwork"))
        if changed or now - self.last_np_write > NOW_PLAYING_REFRESH:
            write_json(self.now_playing_path, record)
            self.last_np_write = now
        title = " – ".join(x for x in (record["title"], record["artist"]) if x) or record["source"]
        self._publish("now_playing", title, {k: record[k] for k in ("source", "title", "artist", "album", "state")})

    def update_hint(self, now):
        """'Play music here' details for the frame: speaker name and YouTube TV code."""
        pairing = read_json(os.path.join(self.runtime, "youtube-pairing.json")) or {}
        hint = {"name": self.s.name, "tv_code": pairing.get("code", "") if self.s.enabled["youtube"] else ""}
        previous = read_json(self.hint_path) or {}
        if hint["tv_code"] != previous.get("tv_code") or hint["name"] != previous.get("name") \
                or now - self.last_hint_write > HINT_REFRESH:
            write_json(self.hint_path, {**hint, "updated": now})
            self.last_hint_write = now

    def _publish(self, key, value, attributes=None):
        if self.mqtt:
            self.mqtt.publish(key, value, attributes)

    def close(self):
        remove(self.now_playing_path)
        remove(self.hint_path)
        if self.mqtt:
            self.mqtt.close()


def main():
    ap = argparse.ArgumentParser(description="NasPicFrame music coordinator")
    ap.add_argument("-c", "--config", default=DEFAULT_CONFIG)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")

    # systemd stops us with SIGTERM: exit through `finally` so the frame's
    # now-playing card is removed rather than left stale.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    coordinator = Coordinator(Settings(args.config))
    try:
        result = coordinator.run()
    finally:
        coordinator.close()
    if result == "restart":
        os.execv(sys.executable, [sys.executable] + sys.argv)


if __name__ == "__main__":
    main()
