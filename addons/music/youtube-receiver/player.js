// yt-cast-receiver Player implementation that plays audio through mpv.

import { Player } from 'yt-cast-receiver';
import { videoUrl } from './util.js';

const LOAD_TIMEOUT_MS = 60000; // yt-dlp can take a while on a Pi 3

export class MpvPlayer extends Player {
  #mpv;
  #volume;
  #loadToken = 0;
  #entryId = null;

  #resolver;

  constructor(mpv, { startVolume, resolver = null }) {
    super();
    this.#mpv = mpv;
    this.#resolver = resolver;
    this.#volume = { level: startVolume, muted: false };
    this.currentVideo = null;
    mpv.on('event', (msg) => {
      // A track ran to its end: move on, as yt-cast-receiver expects us to.
      if (msg.event === 'end-file' && msg.reason === 'eof' && msg.playlist_entry_id === this.#entryId) {
        this.emit('finished');
      }
    });
  }

  // Resolves true once mpv has loaded the stream, false on error, timeout or when
  // superseded by a newer play request.
  #waitForLoad(token) {
    return new Promise((resolve) => {
      let entry = null;
      const done = (result) => {
        clearTimeout(timer);
        this.#mpv.off('event', onEvent);
        resolve(result && token === this.#loadToken);
      };
      const onEvent = (msg) => {
        if (msg.event === 'start-file' && entry === null) {
          entry = msg.playlist_entry_id;
          this.#entryId = entry;
        } else if (msg.event === 'file-loaded' && entry !== null) {
          done(true);
        } else if (msg.event === 'end-file' && msg.playlist_entry_id === entry && msg.reason !== 'eof') {
          done(false); // 'error', or 'stop' when cancelled
        }
      };
      const timer = setTimeout(() => done(false), LOAD_TIMEOUT_MS);
      this.#mpv.on('event', onEvent);
    });
  }

  async doPlay(video, position) {
    const token = ++this.#loadToken;
    this.currentVideo = video;
    const log = this.logger ?? console;

    // Preferred: the stream URL from the resolver (usually prefetched, so instant).
    // Fallback: the watch URL, which mpv resolves itself through yt-dlp.
    let url = null;
    if (this.#resolver) {
      try {
        url = await this.#resolver.get(video.id);
      } catch (error) {
        log.warn(`[picframe] Lookup failed for ${video.id} (${error.message}); letting mpv try`);
      }
      if (token !== this.#loadToken) return false; // superseded while looking up
    }
    if (url && (await this.#load(url, token))) return this.#started(position);
    if (token !== this.#loadToken) return false;
    if (url) this.#resolver.forget(video.id); // e.g. an expired link

    if (await this.#load(videoUrl(video.id), token)) return this.#started(position);
    if (token === this.#loadToken) {
      log.warn(`[picframe] Could not play ${video.id}: yt-dlp failed or timed out (test it with /opt/picframe-music/update-ytdlp.sh)`);
    }
    return false;
  }

  async #load(url, token) {
    const loaded = this.#waitForLoad(token);
    try {
      await this.#mpv.setProperty('pause', false);
      await this.#mpv.command('loadfile', url, 'replace');
    } catch (error) {
      (this.logger ?? console).error('[picframe] loadfile failed:', error);
      return false;
    }
    return loaded;
  }

  async #started(position) {
    if (position > 0) {
      await this.#mpv.command('seek', position, 'absolute').catch(() => {});
    }
    return true;
  }

  async doPause() {
    return this.#set('pause', true);
  }

  async doResume() {
    return this.#set('pause', false);
  }

  async doStop() {
    this.#loadToken++; // cancels a pending load
    this.currentVideo = null;
    try {
      await this.#mpv.command('stop');
      return true;
    } catch {
      return false;
    }
  }

  async doSeek(position) {
    try {
      await this.#mpv.command('seek', position, 'absolute');
      return true;
    } catch {
      return false;
    }
  }

  async doSetVolume(volume) {
    const ok = (await this.#set('volume', volume.level)) && (await this.#set('mute', volume.muted));
    if (ok) this.#volume = { level: volume.level, muted: volume.muted };
    return ok;
  }

  async doGetVolume() {
    return this.#volume;
  }

  async doGetPosition() {
    return (await this.#mpv.getProperty('time-pos')) ?? 0;
  }

  async doGetDuration() {
    return (await this.#mpv.getProperty('duration')) ?? 0;
  }

  async #set(name, value) {
    try {
      await this.#mpv.setProperty(name, value);
      return true;
    } catch {
      return false;
    }
  }
}
