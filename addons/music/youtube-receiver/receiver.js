// NasPicFrame YouTube / YouTube Music cast receiver.
//
// Appears in the cast menu of the YouTube and YouTube Music apps (DIAL discovery on
// the local network, or "Link with TV code"), and plays the audio through mpv + yt-dlp.
// Writes what's playing and the current TV code to $MUSIC_RUNTIME_DIR for the
// coordinator, and listens on localhost for the coordinator's pause/stop requests.

import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import YouTubeCastReceiver from 'yt-cast-receiver';
import { MpvClient } from './mpv.js';
import { MpvPlayer } from './player.js';
import {
  DIAL_PORT, artworkUrl, cleanArtist, formatPairingCode, mpvArgs, readConfig, stateName,
} from './util.js';

const cfg = readConfig(process.env);
const log = (...args) => console.log('[picframe-youtube]', ...args);
fs.mkdirSync(cfg.runtimeDir, { recursive: true });
const statePath = path.join(cfg.runtimeDir, 'youtube.json');
const pairingPath = path.join(cfg.runtimeDir, 'youtube-pairing.json');

function writeJson(file, data) {
  const tmp = `${file}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(data));
  fs.renameSync(tmp, file);
}

// ---------------------------------------------------------------- player

const mpv = new MpvClient(
  mpvArgs({ socketPath: path.join(cfg.runtimeDir, 'mpv.sock'), ytdlpPath: cfg.ytdlpPath,
            startVolume: cfg.startVolume }),
  path.join(cfg.runtimeDir, 'mpv.sock'));
mpv.on('exit', (code, signal) => {
  log(`mpv exited (${code ?? signal}) - restarting via systemd`);
  process.exit(1);
});
await mpv.start();

const player = new MpvPlayer(mpv, { startVolume: cfg.startVolume });
player.on('finished', async () => {
  await player.pause();
  await player.next();
});

// ---------------------------------------------------------------- now playing

const metadataCache = new Map(); // videoId -> {title, artist}

// Title and artist from YouTube's public oEmbed endpoint (no API key needed).
async function metadataFor(videoId) {
  if (metadataCache.has(videoId)) return metadataCache.get(videoId);
  let meta = { title: '', artist: '' };
  try {
    const url = `https://www.youtube.com/oembed?format=json&url=${encodeURIComponent(`https://www.youtube.com/watch?v=${videoId}`)}`;
    const res = await fetch(url, { signal: AbortSignal.timeout(8000) });
    if (res.ok) {
      const data = await res.json();
      meta = { title: data.title || '', artist: cleanArtist(data.author_name) };
    }
  } catch (error) {
    log(`No metadata for ${videoId}: ${error.message}`);
  }
  if (!meta.title) meta.title = (await mpv.getProperty('media-title')) || '';
  metadataCache.set(videoId, meta);
  return meta;
}

let lastActive = Date.now();

player.on('state', async ({ current }) => {
  const state = stateName(current.status);
  if (state === 'playing' || state === 'loading') lastActive = Date.now();
  // The queue's current video is already the new one while it's still loading.
  const video = current.queue?.current ?? player.currentVideo;
  if (!state || !video) {
    fs.rmSync(statePath, { force: true });
    return;
  }
  const meta = await metadataFor(video.id);
  writeJson(statePath, {
    state, ...meta, album: '', artwork_url: artworkUrl(video.id), video_id: video.id,
    updated: Date.now() / 1000,
  });
});

// ---------------------------------------------------------------- receiver

const receiver = new YouTubeCastReceiver(player, {
  device: { name: cfg.name, screenName: cfg.name, brand: 'NasPicFrame', model: 'Picture Frame' },
  dial: { port: DIAL_PORT },
  logLevel: 'info',
});
receiver.on('senderConnect', (sender) => {
  log(`Connected: ${sender.name ?? 'unknown sender'} (${receiver.getConnectedSenders().length} connected)`);
});
receiver.on('senderDisconnect', (sender, implicit) => {
  log(`Disconnected: ${sender.name ?? 'unknown sender'}${implicit ? ' (lost connection)' : ''}`);
});
receiver.on('error', (error) => log('Receiver error:', error.message));
receiver.on('terminate', (error) => {
  log('Receiver terminated:', error?.message);
  process.exit(1);
});
await receiver.start();
log(`Ready as "${cfg.name}" (DIAL port ${DIAL_PORT})`);

// "Link with TV code": lets guests on another network (or mobile data) connect.
// Codes refresh every five minutes; the service stops on error, so restart it.
function startPairing() {
  const service = receiver.getPairingCodeRequestService();
  service.on('response', (code) => {
    writeJson(pairingPath, { code: formatPairingCode(code), updated: Date.now() / 1000 });
  });
  service.on('error', (error) => {
    log('Pairing code unavailable:', error.message);
    fs.rmSync(pairingPath, { force: true });
    setTimeout(startPairing, 60000);
  });
  service.start();
}
startPairing();

// ---------------------------------------------------------------- idle queue clearing

if (cfg.idleClearMinutes > 0) {
  setInterval(async () => {
    const playing = [1, 3].includes(player.status); // PLAYING or LOADING
    const idleMs = Date.now() - lastActive;
    if (!playing && idleMs > cfg.idleClearMinutes * 60000 && player.queue.videoIds.length) {
      log(`Idle for ${cfg.idleClearMinutes} min - clearing the queue`);
      await player.reset();
      lastActive = Date.now();
    }
  }, 60000);
}

// ---------------------------------------------------------------- control (localhost)

http.createServer(async (req, res) => {
  let ok = true;
  if (req.method === 'POST' && req.url === '/pause') ok = await player.pause();
  else if (req.method === 'POST' && req.url === '/stop') ok = await player.stop();
  else if (req.method !== 'GET' || req.url !== '/state') {
    res.writeHead(404).end();
    return;
  }
  res.writeHead(200, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ ok, status: player.status }));
}).listen(cfg.controlPort, '127.0.0.1');

// ---------------------------------------------------------------- shutdown

async function shutdown() {
  log('Stopping');
  fs.rmSync(statePath, { force: true });
  fs.rmSync(pairingPath, { force: true });
  try {
    await receiver.stop();
  } catch { /* already stopped */ }
  mpv.kill();
  process.exit(0);
}
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
