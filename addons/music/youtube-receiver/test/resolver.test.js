import assert from 'node:assert/strict';
import { test } from 'node:test';
import { StreamResolver } from '../resolver.js';

function fakeRun({ fail = new Set() } = {}) {
  const calls = [];
  const run = (id) => {
    calls.push(id);
    return new Promise((resolve, reject) => setImmediate(() => (
      fail.has(id) ? reject(new Error(`no ${id}`)) : resolve(`https://media.example/${id}`))));
  };
  return { run, calls };
}

test('concurrent requests share one lookup (play -> stop -> play)', async () => {
  const { run, calls } = fakeRun();
  const resolver = new StreamResolver({ run });
  const [a, b] = await Promise.all([resolver.get('x'), resolver.get('x')]);
  assert.equal(a, 'https://media.example/x');
  assert.equal(b, a);
  assert.deepEqual(calls, ['x']);
});

test('a prefetched song is not looked up again', async () => {
  const { run, calls } = fakeRun();
  const resolver = new StreamResolver({ run });
  resolver.prefetch('next');
  assert.equal(await resolver.get('next'), 'https://media.example/next');
  assert.deepEqual(calls, ['next']);
});

test('failures are not cached', async () => {
  const fail = new Set(['bad']);
  const { run, calls } = fakeRun({ fail });
  const resolver = new StreamResolver({ run });
  await assert.rejects(resolver.get('bad'));
  fail.delete('bad');
  assert.equal(await resolver.get('bad'), 'https://media.example/bad');
  assert.equal(calls.length, 2);
});

test('forget drops a cached URL', async () => {
  const { run, calls } = fakeRun();
  const resolver = new StreamResolver({ run });
  await resolver.get('x');
  resolver.forget('x');
  await resolver.get('x');
  assert.equal(calls.length, 2);
});

test('prefetch ignores empty ids and swallows errors', async () => {
  const { run, calls } = fakeRun({ fail: new Set(['bad']) });
  const resolver = new StreamResolver({ run });
  resolver.prefetch(undefined);
  resolver.prefetch('bad');
  await new Promise((r) => setTimeout(r, 10));
  assert.deepEqual(calls, ['bad']);
  assert.equal(resolver.has('bad'), false);
});
