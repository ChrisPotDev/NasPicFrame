import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { test } from 'node:test';
import { MpvPlayer } from '../player.js';

// Stands in for MpvClient: records commands and replays mpv's events for loadfile.
class FakeMpv extends EventEmitter {
  constructor({ loadSucceeds = true } = {}) {
    super();
    this.commands = [];
    this.props = {};
    this.loadSucceeds = loadSucceeds;
    this.entry = 0;
  }

  async command(...args) {
    this.commands.push(args);
    if (args[0] === 'loadfile') {
      const id = ++this.entry;
      setImmediate(() => {
        this.emit('event', { event: 'start-file', playlist_entry_id: id });
        if (this.loadSucceeds) this.emit('event', { event: 'file-loaded' });
        else this.emit('event', { event: 'end-file', reason: 'error', playlist_entry_id: id });
      });
    }
  }

  async setProperty(name, value) {
    this.props[name] = value;
  }

  async getProperty(name) {
    return this.props[name] ?? null;
  }
}

test('doPlay loads the video URL and seeks to the start position', async () => {
  const mpv = new FakeMpv();
  const player = new MpvPlayer(mpv, { startVolume: 40 });
  assert.equal(await player.doPlay({ id: 'abc' }, 12), true);
  assert.deepEqual(mpv.commands[0], ['loadfile', 'https://www.youtube.com/watch?v=abc', 'replace']);
  assert.deepEqual(mpv.commands[1], ['seek', 12, 'absolute']);
  assert.equal(mpv.props.pause, false);
});

test('doPlay reports failure when yt-dlp cannot load the stream', async () => {
  const player = new MpvPlayer(new FakeMpv({ loadSucceeds: false }), { startVolume: 40 });
  assert.equal(await player.doPlay({ id: 'broken' }, 0), false);
});

test('end of track is reported only for the current entry', async () => {
  const mpv = new FakeMpv();
  const player = new MpvPlayer(mpv, { startVolume: 40 });
  await player.doPlay({ id: 'abc' }, 0);
  let finished = 0;
  player.on('finished', () => finished++);
  mpv.emit('event', { event: 'end-file', reason: 'eof', playlist_entry_id: 99 });
  mpv.emit('event', { event: 'end-file', reason: 'stop', playlist_entry_id: 1 });
  mpv.emit('event', { event: 'end-file', reason: 'eof', playlist_entry_id: 1 });
  assert.equal(finished, 1);
});

test('volume, pause and position', async () => {
  const mpv = new FakeMpv();
  const player = new MpvPlayer(mpv, { startVolume: 40 });
  assert.deepEqual(await player.doGetVolume(), { level: 40, muted: false });
  assert.equal(await player.doSetVolume({ level: 65, muted: true }), true);
  assert.equal(mpv.props.volume, 65);
  assert.equal(mpv.props.mute, true);
  assert.deepEqual(await player.doGetVolume(), { level: 65, muted: true });
  await player.doPause();
  assert.equal(mpv.props.pause, true);
  assert.equal(await player.doGetPosition(), 0);
  mpv.props['time-pos'] = 31.5;
  assert.equal(await player.doGetPosition(), 31.5);
});

test('doStop cancels a pending load', async () => {
  const mpv = new FakeMpv();
  mpv.loadSucceeds = true;
  const player = new MpvPlayer(mpv, { startVolume: 40 });
  const playing = player.doPlay({ id: 'abc' }, 0);
  await player.doStop();
  assert.equal(await playing, false);
  assert.ok(mpv.commands.some((c) => c[0] === 'stop'));
});
