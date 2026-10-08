// Pure helpers for the YouTube receiver (no I/O), so they can be unit tested.

export const DIAL_PORT = 3000;

export function readConfig(env) {
  const cfg = JSON.parse(env.MUSIC_CONFIG || '{}');
  return {
    name: cfg.names?.youtube || 'Living Room',
    startVolume: clampPercent(cfg.start_volume ?? 40),
    idleClearMinutes: Math.max(0, Number(cfg.idle_clear_minutes ?? 30)),
    runtimeDir: env.MUSIC_RUNTIME_DIR || '/tmp/picframe-music',
    ytdlpPath: env.YTDLP_PATH || 'yt-dlp',
    controlPort: Number(env.CONTROL_PORT || 8766),
  };
}

export function clampPercent(value) {
  const n = Number(value);
  return Number.isFinite(n) ? Math.max(0, Math.min(100, Math.round(n))) : 0;
}

// mpv runs once for the receiver's lifetime and is driven over its JSON IPC socket.
export function mpvArgs({ socketPath, ytdlpPath, startVolume }) {
  return [
    '--idle=yes',
    '--no-video',
    '--no-terminal',
    `--input-ipc-server=${socketPath}`,
    // PulseAudio API (served by PipeWire), so the coordinator can see this stream
    // under a fixed name and route it to the DAC or the headphone jack.
    '--ao=pulse',
    '--audio-client-name=picframe-youtube',
    `--volume=${clampPercent(startVolume)}`,
    '--volume-max=100',
    '--ytdl-format=bestaudio/best',
    `--script-opts=ytdl_hook-ytdl_path=${ytdlpPath}`,
    // Current yt-dlp needs a JavaScript runtime for YouTube; Node is already here.
    '--ytdl-raw-options=js-runtimes=node',
    // Keep memory use modest on a 1 GB Pi.
    '--demuxer-max-bytes=20MiB',
    '--demuxer-max-back-bytes=5MiB',
  ];
}

export function videoUrl(videoId) {
  return `https://www.youtube.com/watch?v=${encodeURIComponent(videoId)}`;
}

export function artworkUrl(videoId) {
  return `https://i.ytimg.com/vi/${encodeURIComponent(videoId)}/hqdefault.jpg`;
}

// YouTube Music's auto-generated artist channels are called "Artist - Topic".
export function cleanArtist(name) {
  return String(name || '').replace(/\s+-\s+Topic$/i, '').trim();
}

// Pairing codes are 12 digits; YouTube shows them in groups of three.
export function formatPairingCode(code) {
  const digits = String(code || '').replace(/\D/g, '');
  return digits.replace(/(\d{3})(?=\d)/g, '$1 ');
}

// Maps yt-cast-receiver PLAYER_STATUSES values to the add-on's state names.
export function stateName(status) {
  switch (status) {
    case 1: return 'playing';
    case 2: return 'paused';
    case 3: return 'loading';
    default: return null; // idle (-1) or stopped (4): nothing to show
  }
}
