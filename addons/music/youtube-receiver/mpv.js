// Minimal client for mpv's JSON IPC: start mpv, send commands, receive events.

import { spawn } from 'node:child_process';
import { EventEmitter } from 'node:events';
import fs from 'node:fs';
import net from 'node:net';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export class MpvClient extends EventEmitter {
  #args;
  #socketPath;
  #proc = null;
  #socket = null;
  #buffer = '';
  #nextId = 1;
  #pending = new Map();

  constructor(args, socketPath) {
    super();
    this.#args = args;
    this.#socketPath = socketPath;
  }

  async start() {
    fs.rmSync(this.#socketPath, { force: true });
    this.#proc = spawn('mpv', this.#args, { stdio: ['ignore', 'inherit', 'inherit'] });
    this.#proc.on('exit', (code, signal) => this.emit('exit', code, signal));
    for (let i = 0; i < 100; i++) { // up to 10 s for mpv to create its socket
      try {
        await this.#connect();
        return;
      } catch {
        await sleep(100);
      }
    }
    throw new Error(`mpv IPC socket ${this.#socketPath} did not appear`);
  }

  #connect() {
    return new Promise((resolve, reject) => {
      const socket = net.createConnection(this.#socketPath);
      socket.setEncoding('utf8');
      socket.once('connect', () => {
        this.#socket = socket;
        socket.on('data', (chunk) => this.#onData(chunk));
        socket.on('close', () => this.emit('exit', null, 'ipc-closed'));
        resolve();
      });
      socket.once('error', reject);
    });
  }

  #onData(chunk) {
    this.#buffer += chunk;
    let newline;
    while ((newline = this.#buffer.indexOf('\n')) >= 0) {
      const line = this.#buffer.slice(0, newline);
      this.#buffer = this.#buffer.slice(newline + 1);
      if (!line.trim()) continue;
      let msg;
      try {
        msg = JSON.parse(line);
      } catch {
        continue;
      }
      if (msg.request_id && this.#pending.has(msg.request_id)) {
        const { resolve, reject, timer } = this.#pending.get(msg.request_id);
        this.#pending.delete(msg.request_id);
        clearTimeout(timer);
        if (msg.error === 'success') resolve(msg.data);
        else reject(new Error(`mpv: ${msg.error}`));
      } else if (msg.event) {
        this.emit('event', msg);
      }
    }
  }

  command(...args) {
    if (!this.#socket) return Promise.reject(new Error('mpv not connected'));
    const id = this.#nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.#pending.delete(id);
        reject(new Error(`mpv command timed out: ${args[0]}`));
      }, 10000);
      this.#pending.set(id, { resolve, reject, timer });
      this.#socket.write(`${JSON.stringify({ command: args, request_id: id })}\n`);
    });
  }

  async getProperty(name) {
    try {
      return await this.command('get_property', name);
    } catch {
      return null; // e.g. 'property unavailable' while nothing is loaded
    }
  }

  setProperty(name, value) {
    return this.command('set_property', name, value);
  }

  kill() {
    this.#socket?.destroy();
    this.#proc?.kill();
  }
}
