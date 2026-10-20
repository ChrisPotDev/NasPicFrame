// Looks up YouTube audio stream URLs with yt-dlp, ahead of time where possible.
//
// On a Pi 3 one lookup takes ~10 s, so the receiver prefetches the next song in the
// queue while the current one plays, reuses a lookup that's already in progress
// (senders often send play -> stop -> play), and caches results for a while.

import { execFile } from 'node:child_process';

const CACHE_TTL_MS = 2 * 60 * 60 * 1000; // stream URLs stay valid for ~6 h
const CACHE_SIZE = 30;
const LOOKUP_TIMEOUT_MS = 90 * 1000;

// Default runner: yt-dlp at low CPU priority, so prefetching never starves playback.
export function ytdlpRunner({ ytdlpPath, jsRuntime }) {
  return (videoId) => new Promise((resolve, reject) => {
    const args = ['-n', '10', ytdlpPath, '--quiet', '--no-warnings', '--no-playlist',
      '--js-runtimes', jsRuntime, '-f', 'bestaudio/best', '--get-url',
      `https://www.youtube.com/watch?v=${videoId}`];
    execFile('nice', args, { timeout: LOOKUP_TIMEOUT_MS }, (error, stdout, stderr) => {
      const url = String(stdout).split('\n')[0].trim();
      if (!error && /^https?:\/\//.test(url)) resolve(url);
      else reject(new Error((stderr || error?.message || 'no URL').toString().trim().split('\n').pop()));
    });
  });
}

export class StreamResolver {
  #run;
  #log;
  #cache = new Map(); // videoId -> { promise, pending, time }

  constructor({ run, log = () => {} }) {
    this.#run = run;
    this.#log = log;
  }

  // Promise of the stream URL. Concurrent and repeated requests share one lookup.
  get(videoId) {
    const hit = this.#cache.get(videoId);
    if (hit && (hit.pending || Date.now() - hit.time < CACHE_TTL_MS)) return hit.promise;

    const started = Date.now();
    const entry = { pending: true, time: started };
    entry.promise = this.#run(videoId).then((url) => {
      entry.pending = false;
      entry.time = Date.now();
      this.#log(`Looked up ${videoId} in ${((Date.now() - started) / 1000).toFixed(1)} s`);
      return url;
    }, (error) => {
      this.#cache.delete(videoId); // don't cache failures
      throw error;
    });
    this.#cache.set(videoId, entry);
    this.#trim();
    return entry.promise;
  }

  // Start a lookup in the background; errors are dealt with when it's played.
  prefetch(videoId) {
    if (videoId) this.get(videoId).catch(() => {});
  }

  // A cached URL failed to play (e.g. expired): look it up afresh next time.
  forget(videoId) {
    this.#cache.delete(videoId);
  }

  has(videoId) {
    return this.#cache.has(videoId);
  }

  #trim() {
    while (this.#cache.size > CACHE_SIZE) {
      this.#cache.delete(this.#cache.keys().next().value); // oldest first
    }
  }
}
