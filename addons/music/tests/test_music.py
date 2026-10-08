"""Tests for the music add-on's Python parts (no audio hardware or services needed)."""

import base64
import datetime as dt
import json
import os
import sys
import textwrap

import pytest

ADDON = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ADDON)
import coordinator  # noqa: E402
import launch  # noqa: E402
import musicconf  # noqa: E402
import spotify_event  # noqa: E402

TEMPLATE = os.path.join(ADDON, "music.ini")
T = dt.time


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------- config

def test_shipped_config_defaults():
    s = musicconf.Settings(TEMPLATE)
    assert s.names == {"youtube": "Living Room", "spotify": "Living Room", "airplay": "Living Room"}
    assert all(s.enabled.values())
    assert (s.max_volume, s.start_volume, s.quiet_max_volume) == (80, 40, 50)
    assert s.quiet_hours == (T(22, 0), T(7, 0))
    assert s.output == "auto" and s.allow_takeover and s.idle_clear_minutes == 30
    assert s.mqtt_enabled is False


def test_per_app_names_and_switches(tmp_path):
    path = write(tmp_path, "music.ini", """\
        [music]
        name = Chris's Lounge 🎶
        spotify_name = Lounge (Spotify)
        airplay = no
        """)
    s = musicconf.Settings(path)
    assert s.names["youtube"] == "Chris's Lounge 🎶"
    assert s.names["spotify"] == "Lounge (Spotify)"
    assert s.enabled == {"youtube": True, "spotify": True, "airplay": False}


@pytest.mark.parametrize("now, cap", [(T(12, 0), 80), (T(21, 59), 80), (T(22, 0), 50),
                                      (T(2, 0), 50), (T(7, 0), 80)])
def test_volume_cap_with_quiet_hours(now, cap):
    assert musicconf.Settings(TEMPLATE).volume_cap(now) == cap


def test_volume_cap_never_exceeds_max(tmp_path):
    path = write(tmp_path, "music.ini", """\
        [volume]
        max_volume = 60
        quiet_hours =
        quiet_max_volume = 90
        """)
    s = musicconf.Settings(path)
    assert s.quiet_hours is None
    assert s.volume_cap(T(23, 0)) == 60


def test_bad_values_are_rejected(tmp_path):
    with pytest.raises(SystemExit):
        musicconf.Settings(write(tmp_path, "a.ini", "[output]\noutput = speakers\n"))
    with pytest.raises(SystemExit):
        musicconf.Settings(write(tmp_path, "b.ini", "[volume]\nquiet_hours = late\n"))


def test_migrate_adds_new_options_and_keeps_values(tmp_path):
    path = write(tmp_path, "music.ini", """\
        ; mine
        [music]
        name = Kitchen
        youtube = no
        """)
    musicconf.migrate_config(path, TEMPLATE)
    text = open(path, encoding="utf-8").read()
    assert "; mine" in text and "[mqtt]" in text and "airplay_password" in text
    s = musicconf.Settings(path)
    assert s.name == "Kitchen" and s.enabled["youtube"] is False
    before = text
    musicconf.migrate_config(path, TEMPLATE)
    assert open(path, encoding="utf-8").read() == before


def test_prefill_mqtt_from_frame(tmp_path):
    music = tmp_path / "music.ini"
    music.write_text(open(TEMPLATE, encoding="utf-8").read(), encoding="utf-8")
    frame = write(tmp_path, "config.ini", """\
        [mqtt]
        enabled = yes
        host = 192.168.1.20
        username = picframe
        password = s3cr%t
        """)
    musicconf.prefill_mqtt(str(music), frame)
    s = musicconf.Settings(str(music))
    assert s.mqtt_enabled and s.mqtt_host == "192.168.1.20"
    assert s.mqtt_username == "picframe" and s.mqtt_password == "s3cr%t"


def test_prefill_mqtt_skipped_when_frame_has_none(tmp_path):
    music = tmp_path / "music.ini"
    music.write_text(open(TEMPLATE, encoding="utf-8").read(), encoding="utf-8")
    frame = write(tmp_path, "config.ini", "[mqtt]\nenabled = no\n")
    musicconf.prefill_mqtt(str(music), frame)
    assert musicconf.Settings(str(music)).mqtt_enabled is False


# --------------------------------------------------------------------------- launcher

def test_spotify_command():
    argv, env = launch.build_command("spotify", musicconf.Settings(TEMPLATE), "/tmp")
    assert argv[0] == "librespot"
    assert argv[argv.index("--name") + 1] == "Living Room"
    assert argv[argv.index("--initial-volume") + 1] == "40"
    assert argv[argv.index("--backend") + 1] == "pulseaudio"
    assert env["PULSE_PROP"] == "application.name=picframe-spotify"


def test_airplay_config_escapes_and_password(tmp_path):
    path = write(tmp_path, "music.ini", """\
        [music]
        name = The "Best" Room
        airplay_password = hunter2
        """)
    argv, _ = launch.build_command("airplay", musicconf.Settings(path), str(tmp_path))
    conf = (tmp_path / "shairport-sync.conf").read_text(encoding="utf-8")
    assert argv == ["shairport-sync", "-c", str(tmp_path / "shairport-sync.conf")]
    assert 'name = "The \\"Best\\" Room";' in conf
    assert 'password = "hunter2";' in conf
    assert 'application_name = "picframe-airplay"' in conf


def test_youtube_command_passes_settings():
    argv, env = launch.build_command("youtube", musicconf.Settings(TEMPLATE), "/run/x")
    assert argv[0] == "node" and argv[1].endswith("receiver.js")
    cfg = json.loads(env["MUSIC_CONFIG"])
    assert cfg["names"]["youtube"] == "Living Room" and cfg["start_volume"] == 40
    assert env["MUSIC_RUNTIME_DIR"] == "/run/x"


# --------------------------------------------------------------------------- Spotify events

def test_spotify_events():
    state = spotify_event.apply_event(None, {
        "PLAYER_EVENT": "track_changed", "NAME": "One More Time",
        "ARTISTS": "Daft Punk\nRomanthony", "ALBUM": "Discovery",
        "COVERS": "https://i.scdn.co/image/a\nhttps://i.scdn.co/image/b"})
    assert state["title"] == "One More Time"
    assert state["artist"] == "Daft Punk, Romanthony"
    assert state["artwork_url"] == "https://i.scdn.co/image/a"
    state = spotify_event.apply_event(state, {"PLAYER_EVENT": "playing"})
    assert state["state"] == "playing" and state["title"] == "One More Time"
    assert spotify_event.apply_event(state, {"PLAYER_EVENT": "stopped"}) is None


def test_spotify_podcast_uses_show_name():
    state = spotify_event.apply_event(None, {"PLAYER_EVENT": "track_changed", "NAME": "Ep 1",
                                             "SHOW_NAME": "A Podcast"})
    assert state["artist"] == "A Podcast"


# --------------------------------------------------------------------------- routing

USB = {"index": 51, "name": "alsa_output.usb-Generic_USB_Audio-00.analog-stereo",
       "description": "USB Audio Analog Stereo", "properties": {"device.bus": "usb"},
       "volume": {"front-left": {"value_percent": "80%"}, "front-right": {"value_percent": "79%"}}}
JACK = {"index": 47, "name": "alsa_output.platform-bcm2835_audio.stereo-fallback",
        "description": "Built-in Audio Stereo", "properties": {"alsa.card_name": "bcm2835 Headphones"}}
HDMI = {"index": 48, "name": "alsa_output.platform-3f902000.hdmi.hdmi-stereo",
        "description": "Built-in Audio Digital Stereo (HDMI)", "properties": {"alsa.card_name": "vc4-hdmi"}}


def test_sink_kinds():
    assert [coordinator.sink_kind(s) for s in (USB, JACK, HDMI)] == ["usb", "headphones", "hdmi"]


def test_choose_sink():
    assert coordinator.choose_sink([HDMI, JACK, USB], "auto") == USB["name"]
    assert coordinator.choose_sink([HDMI, JACK], "auto") == JACK["name"]  # DAC unplugged
    assert coordinator.choose_sink([HDMI, JACK], "usb") is None
    assert coordinator.choose_sink([HDMI, JACK, USB], "headphones") == JACK["name"]
    assert coordinator.choose_sink([HDMI, JACK, USB], "hdmi") == HDMI["name"]
    assert coordinator.choose_sink([USB], "usb", usb_device="generic") == USB["name"]
    assert coordinator.choose_sink([USB], "usb", usb_device="fiio") is None


def test_sink_volume_percent():
    assert coordinator.sink_volume_percent(USB) == 80
    assert coordinator.sink_volume_percent(JACK) is None


def test_playing_sources():
    inputs = [
        {"corked": False, "properties": {"application.name": "picframe-youtube"}},
        {"corked": True, "properties": {"application.name": "picframe-airplay"}},
        {"corked": False, "properties": {"application.process.binary": "librespot"}},
        {"corked": False, "properties": {"application.name": "Firefox"}},
    ]
    assert coordinator.playing_sources(inputs) == {"youtube", "spotify"}


# --------------------------------------------------------------------------- takeover

def test_decide_single_and_idle():
    assert coordinator.decide({}, None, False, True) == (None, [])
    assert coordinator.decide({"spotify": 1.0}, None, False, True) == ("spotify", [])


def test_decide_latest_source_wins():
    keep, stop = coordinator.decide({"youtube": 1.0, "airplay": 5.0}, "youtube", False, True)
    assert keep == "airplay" and stop == ["youtube"]


def test_decide_lock_keeps_current():
    keep, stop = coordinator.decide({"youtube": 1.0, "airplay": 5.0}, "youtube", True, True)
    assert keep == "youtube" and stop == ["airplay"]


def test_decide_without_takeover_keeps_first():
    keep, stop = coordinator.decide({"youtube": 3.0, "spotify": 2.0}, None, False, False)
    assert keep == "spotify" and stop == ["youtube"]


# --------------------------------------------------------------------------- AirPlay metadata

def item(kind, code, data=None):
    hexed = kind.encode().hex() + "</type><code>" + code.encode().hex()
    if data is None:
        return f"<item><type>{hexed}</code><length>0</length></item>"
    b64 = base64.b64encode(data).decode()
    return (f"<item><type>{hexed}</code><length>{len(data)}</length>\n"
            f"<data encoding=\"base64\">\n{b64}</data></item>\n")


def test_parse_metadata_items_keeps_partial_tail():
    stream = item("core", "minm", b"Song") + item("ssnc", "pbeg") + "<item><type>636f"
    items, rest = coordinator.parse_metadata_items(stream)
    assert items == [("core", "minm", b"Song"), ("ssnc", "pbeg", b"")]
    assert rest.startswith("<item><type>636f")


def test_airplay_metadata_state(tmp_path):
    md = coordinator.AirPlayMetadata(str(tmp_path))
    md.apply("core", "minm", "Titel".encode())
    md.apply("core", "asar", b"Artist")
    md.apply("ssnc", "pbeg", b"")
    md.apply("ssnc", "PICT", b"\xff\xd8\xff fake jpeg")
    state = json.loads((tmp_path / "airplay.json").read_text())
    assert state["title"] == "Titel" and state["artist"] == "Artist" and state["state"] == "playing"
    assert state["artwork_file"].endswith(".jpg") and os.path.exists(state["artwork_file"])
    md.apply("ssnc", "pend", b"")
    assert not (tmp_path / "airplay.json").exists()


def test_now_playing_record():
    assert coordinator.now_playing_from(None, {"title": "x"}, None) is None
    rec = coordinator.now_playing_from("spotify", {"state": "playing", "title": "T", "artist": "A"},
                                       "/run/art")
    assert rec["source"] == "spotify" and rec["artwork"] == "/run/art" and rec["album"] == ""
