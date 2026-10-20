"""Tests for the parts of picframe.py that don't need a display, a NAS or a broker."""

import datetime as dt
import json
import os
import sys
import textwrap
import time

import pygame
import pytest
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import picframe  # noqa: E402

REPO = os.path.join(os.path.dirname(__file__), "..")
TEMPLATE = os.path.join(REPO, "config.ini")


def write_config(tmp_path, text):
    path = tmp_path / "config.ini"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return str(path)


def make_photo(path, size, orientation=None, taken=None):
    exif = Image.Exif()
    if orientation:
        exif[picframe.TAG_ORIENTATION] = orientation
    if taken:
        exif[picframe.TAG_DATETIME] = taken
    Image.new("RGB", size, (200, 120, 40)).save(path, "JPEG", exif=exif.tobytes())
    return str(path)


# --------------------------------------------------------------------------- schedule

T = dt.time


@pytest.mark.parametrize("now, expected", [
    (T(6, 59), False), (T(7, 0), True), (T(12, 0), True), (T(21, 59), True), (T(22, 0), False),
])
def test_is_awake_daytime(now, expected):
    assert picframe.is_awake(T(7, 0), T(22, 0), now) is expected


@pytest.mark.parametrize("now, expected", [
    (T(17, 59), False), (T(18, 0), True), (T(23, 30), True), (T(0, 59), True), (T(1, 0), False),
])
def test_is_awake_across_midnight(now, expected):
    assert picframe.is_awake(T(18, 0), T(1, 0), now) is expected


def test_is_awake_without_schedule():
    assert picframe.is_awake(None, None, T(3, 0))
    assert picframe.is_awake(T(7, 0), T(7, 0), T(3, 0))


# --------------------------------------------------------------------------- settings

def test_settings_defaults_and_multiple_folders(tmp_path):
    path = write_config(tmp_path, """\
        [slideshow]
        image_dir = /mnt/photos
            /home/pi/Pictures, /media/usb
        """)
    s = picframe.Settings(path)
    assert s.image_dirs == ["/mnt/photos", "/home/pi/Pictures", "/media/usb"]
    assert s.delay == 30
    assert s.date_format == "%-d %B %Y"  # '%' must survive (no interpolation)
    assert s.mqtt_enabled is False
    assert s.wake_time == T(7, 0) and s.sleep_time == T(22, 0)


def test_settings_shipped_config_parses():
    s = picframe.Settings(TEMPLATE)
    assert s.image_dirs == ["/mnt/photos"]
    assert s.placeholder_image == ""


def test_settings_rejects_bad_choice(tmp_path):
    path = write_config(tmp_path, "[slideshow]\nfit = stretch\n")
    with pytest.raises(SystemExit):
        picframe.Settings(path)


# --------------------------------------------------------------------------- config migration

V1_CONFIG = """\
    ; my frame
    [slideshow]
    ; where my photos live
    image_dir = /mnt/nas/family
    delay = 12

    [schedule]
    wake_time = 06:30
    sleep_time = 23:00
    """


def test_migrate_adds_missing_and_keeps_values(tmp_path):
    path = write_config(tmp_path, V1_CONFIG)
    assert picframe.migrate_config(path, TEMPLATE) == 0

    text = open(path, encoding="utf-8").read()
    assert "; my frame" in text and "; where my photos live" in text  # comments kept
    s = picframe.Settings(path)
    assert s.image_dirs == ["/mnt/nas/family"] and s.delay == 12  # values kept
    assert s.wake_time == T(6, 30)
    for section in ("[overlay]", "[display]", "[mqtt]"):
        assert section in text
    assert "pair_portraits = yes" in text
    assert list(tmp_path.glob("config.ini.bak-*"))  # backup written


def test_migrate_is_idempotent(tmp_path):
    path = write_config(tmp_path, V1_CONFIG)
    picframe.migrate_config(path, TEMPLATE)
    before = open(path, encoding="utf-8").read()
    mtime = os.stat(path).st_mtime_ns
    picframe.migrate_config(path, TEMPLATE)
    assert open(path, encoding="utf-8").read() == before
    assert os.stat(path).st_mtime_ns == mtime  # untouched: no restart of the slideshow


def test_migrate_shipped_config_is_up_to_date(tmp_path, capsys):
    path = tmp_path / "config.ini"
    path.write_text(open(TEMPLATE, encoding="utf-8").read(), encoding="utf-8")
    picframe.migrate_config(str(path), TEMPLATE)
    assert "up to date" in capsys.readouterr().out


def test_migrate_keeps_new_options_inside_their_section(tmp_path):
    path = write_config(tmp_path, V1_CONFIG)
    picframe.migrate_config(path, TEMPLATE)
    lines = open(path, encoding="utf-8").read().splitlines()
    layout = picframe._ini_layout(lines)
    assert "matting" in layout["slideshow"]["keys"]
    assert layout["slideshow"]["keys"]["matting"] < layout["schedule"]["start"]


# --------------------------------------------------------------------------- photos

def test_gps_degrees():
    assert picframe._gps_degrees((48.0, 51.0, 30.0), "N") == pytest.approx(48.858333, abs=1e-5)
    assert picframe._gps_degrees((2.0, 17.0, 40.0), b"W") == pytest.approx(-2.294444, abs=1e-5)
    assert picframe._gps_degrees(None, "N") is None


def test_read_exif_date():
    exif = Image.Exif()
    exif[picframe.TAG_DATETIME] = "2019:08:12 14:33:10"
    taken, gps = picframe.read_exif_meta(exif)
    assert taken == dt.datetime(2019, 8, 12, 14, 33, 10)
    assert gps is None


def test_format_coords():
    assert picframe.format_coords(48.8584, -2.2945) == "48.8584°N 2.2945°W"


def test_panel_rects():
    assert picframe.panel_rects((1920, 1080), 1, 8) == [(0, 0, 1920, 1080)]
    left, right = picframe.panel_rects((1920, 1080), 2, 8)
    assert left == (0, 0, 956, 1080) and right == (964, 0, 956, 1080)
    top, bottom = picframe.panel_rects((1080, 1920), 2, 8)
    assert top[3] == bottom[3] == 956 and bottom[1] == 964


def test_render_portrait_pair_with_caption(tmp_path):
    settings = picframe.Settings(TEMPLATE)
    a = make_photo(tmp_path / "a.jpg", (300, 400), taken="2020:01:02 03:04:05")
    b = make_photo(tmp_path / "b.jpg", (300, 400))
    slide = picframe.render_slide((a, b), (640, 360), settings, None)
    assert slide.image.size == (640, 360)
    assert slide.paths == (a, b)
    assert [p.caption for p in slide.panels] == ["2 January 2020", ""]


def test_render_skips_unreadable(tmp_path):
    settings = picframe.Settings(TEMPLATE)
    good = make_photo(tmp_path / "good.jpg", (400, 300))
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not a jpeg")
    slide = picframe.render_slide((str(bad), good), (640, 360), settings, None)
    assert slide.paths == (good,)


def test_builtin_placeholder():
    image, icon_bottom = picframe.builtin_placeholder((1280, 720))
    assert image.size == (1280, 720)
    assert 360 < icon_bottom < 720


# --------------------------------------------------------------------------- library

def test_library_scans_folders_and_detects_orientation(tmp_path):
    nas, local = tmp_path / "nas", tmp_path / "local"
    (nas / "2024").mkdir(parents=True)
    (nas / "@eaDir").mkdir()
    local.mkdir()
    landscape = make_photo(nas / "2024" / "land.jpg", (400, 300))
    rotated = make_photo(local / "rotated.jpg", (400, 300), orientation=6)  # displays as portrait
    make_photo(nas / "@eaDir" / "thumb.jpg", (40, 30))
    (local / "notes.txt").write_text("x")

    path = write_config(tmp_path, f"[slideshow]\nimage_dir = {nas}, {local}\n")
    library = picframe.ImageLibrary(picframe.Settings(path))
    deadline = time.monotonic() + 10
    while not library._files and time.monotonic() < deadline:
        time.sleep(0.05)

    assert sorted(library._files) == sorted([landscape, rotated])
    assert {library.next(), library.next()} == {landscape, rotated}
    assert library.is_portrait(rotated) is True
    assert library.is_portrait(landscape) is False


# --------------------------------------------------------------------------- music add-on status

def test_music_status_reads_fresh_files_and_ignores_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    (tmp_path / "picframe").mkdir()
    status = picframe.MusicStatus()
    assert status.refresh(now=1000) is False and status.now_playing is None  # no add-on

    np = {"state": "playing", "source": "spotify", "title": "T", "artist": "A", "updated": 990}
    (tmp_path / "picframe" / "nowplaying.json").write_text(json.dumps(np))
    (tmp_path / "picframe" / "music-hint.json").write_text(
        json.dumps({"name": "Living Room", "tv_code": "123 456", "updated": 990}))
    assert status.refresh(now=1000) is True
    assert status.now_playing["title"] == "T"
    assert status.hint_text() == "Cast music to \u201cLiving Room\u201d  \u00b7  YouTube TV code 123 456"

    np["updated"] = 1000  # only the timestamp changed: no redraw needed
    (tmp_path / "picframe" / "nowplaying.json").write_text(json.dumps(np))
    assert status.refresh(now=1005) is False

    assert status.refresh(now=1000 + picframe.NOW_PLAYING_MAX_AGE + 1) is True  # stale
    assert status.now_playing is None


def test_ellipsize():
    pygame.font.init()
    font = pygame.font.Font(None, 20)
    assert picframe.ellipsize(font, "short", 500) == "short"
    long = picframe.ellipsize(font, "a very long track title that will not fit", 80)
    assert long.endswith("\u2026") and font.size(long)[0] <= 80


# --------------------------------------------------------------------------- clock layout

def test_clock_size_parsing():
    assert picframe.parse_clock_size("medium") == pytest.approx(1 / 14)
    assert picframe.parse_clock_size(" Large ") == pytest.approx(1 / 9)
    assert picframe.parse_clock_size("12%") == pytest.approx(0.12)
    for bad in ("giant", "1%", "60%", ""):
        with pytest.raises(SystemExit):
            picframe.parse_clock_size(bad)


def test_clock_settings_defaults_and_overrides(tmp_path):
    s = picframe.Settings(TEMPLATE)
    assert s.clock_position == "bottom-left" and s.show_date is False
    assert s.clock_size == pytest.approx(1 / 14)  # unchanged from before the option existed
    path = write_config(tmp_path, "[overlay]\nclock_size = huge\nclock_position = top-center\nshow_date = yes\n")
    s = picframe.Settings(path)
    assert s.clock_size == pytest.approx(1 / 6) and s.clock_position == "top-center" and s.show_date


@pytest.mark.parametrize("position, topleft", [
    ("bottom-left", (20, 1080 - 20 - 130)), ("bottom-right", (1920 - 20 - 300, 930)),
    ("top-center", ((1920 - 300) // 2, 20)), ("top-right", (1600, 20)),
])
def test_place_block_positions(position, topleft):
    block, lines = picframe.place_block([(300, 100), (200, 30)], position, (1920, 1080), 20)
    assert block.size == (300, 130) and block.topleft == topleft
    assert lines[1].top == lines[0].bottom  # date line directly under the time


def test_place_block_centres_lines():
    block, (time_line, date_line) = picframe.place_block(
        [(300, 100), (200, 30)], "bottom-center", (1920, 1080), 20)
    assert time_line.centerx == date_line.centerx == block.centerx


def test_clear_of_moves_away_from_clock():
    clock = pygame.Rect(1600, 930, 300, 130)  # bottom-right
    caption = pygame.Rect(1500, 1000, 400, 40)
    moved = picframe.clear_of(caption, clock, 1080, 10)
    assert moved.bottom == clock.top - 10 and not moved.colliderect(clock)
    top_clock = pygame.Rect(20, 20, 300, 130)
    card = pygame.Rect(0, 20, 500, 80)
    assert picframe.clear_of(card, top_clock, 1080, 10).top == top_clock.bottom + 10
    assert picframe.clear_of(caption, None, 1080, 10) == caption
    elsewhere = pygame.Rect(20, 20, 100, 40)
    assert picframe.clear_of(elsewhere, clock, 1080, 10) == elsewhere
