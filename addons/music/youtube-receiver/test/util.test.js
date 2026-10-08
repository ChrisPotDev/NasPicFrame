import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  artworkUrl, clampPercent, cleanArtist, formatPairingCode, mpvArgs, readConfig, stateName, videoUrl,
} from '../util.js';

test('readConfig uses the YouTube name and defaults', () => {
  const cfg = readConfig({
    MUSIC_CONFIG: JSON.stringify({ names: { youtube: 'Lounge' }, start_volume: 55, idle_clear_minutes: 0 }),
    MUSIC_RUNTIME_DIR: '/run/user/1000/picframe-music',
    CONTROL_PORT: '9000',
  });
  assert.equal(cfg.name, 'Lounge');
  assert.equal(cfg.startVolume, 55);
  assert.equal(cfg.idleClearMinutes, 0);
  assert.equal(cfg.controlPort, 9000);
  assert.equal(readConfig({}).name, 'Living Room');
});

test('clampPercent', () => {
  assert.equal(clampPercent(150), 100);
  assert.equal(clampPercent(-3), 0);
  assert.equal(clampPercent('42.4'), 42);
  assert.equal(clampPercent('x'), 0);
});

test('mpv args route audio through PulseAudio under a fixed name', () => {
  const args = mpvArgs({ socketPath: '/tmp/s', ytdlpPath: '/venv/bin/yt-dlp', startVolume: 40 });
  assert.ok(args.includes('--ao=pulse'));
  assert.ok(args.includes('--audio-client-name=picframe-youtube'));
  assert.ok(args.includes('--input-ipc-server=/tmp/s'));
  assert.ok(args.includes('--script-opts=ytdl_hook-ytdl_path=/venv/bin/yt-dlp'));
  assert.ok(args.includes('--volume=40'));
  assert.ok(args.includes('--no-video'));
});

test('URLs', () => {
  assert.equal(videoUrl('abc_123'), 'https://www.youtube.com/watch?v=abc_123');
  assert.equal(artworkUrl('abc'), 'https://i.ytimg.com/vi/abc/hqdefault.jpg');
});

test('cleanArtist strips YouTube Music "- Topic" channels', () => {
  assert.equal(cleanArtist('Daft Punk - Topic'), 'Daft Punk');
  assert.equal(cleanArtist('Some Channel'), 'Some Channel');
  assert.equal(cleanArtist(undefined), '');
});

test('formatPairingCode groups digits in threes', () => {
  assert.equal(formatPairingCode('123456789012'), '123 456 789 012');
  assert.equal(formatPairingCode('123-456'), '123 456');
});

test('stateName maps player statuses', () => {
  assert.equal(stateName(1), 'playing');
  assert.equal(stateName(2), 'paused');
  assert.equal(stateName(3), 'loading');
  assert.equal(stateName(4), null);
  assert.equal(stateName(-1), null);
});
