import assert from 'node:assert/strict';
import {test} from 'node:test';
import {createDisplayBuilder} from '../frontend/src/displayContent.js';

test('unchanged displayed messages and process groups retain identity', () => {
  const build = createDisplayBuilder(new Set(['command', 'reasoning']));
  const user = {id: 'u', type: 'user', text: 'question'};
  const command = {id: 'c', type: 'command', text: 'output'};
  const answer = {id: 'a', type: 'agent', text: 'answer'};
  const first = build([user, command, answer]);
  const next = build([user, command, {...answer, text: 'answer extended'}]);
  assert.equal(next[0], first[0]);
  assert.equal(next[1], first[1]);
  assert.notEqual(next[2], first[2]);
  assert.equal(first[2].text, 'answer');
  const later = build([user, command, answer, {id: 'u2', type: 'user', text: 'next question'}]);
  assert.equal(later[0], next[0]);
  assert.equal(later[1], next[1]);
});

test('new answers update question links and new process output updates groups', () => {
  const build = createDisplayBuilder(new Set(['command']));
  const user = {id: 'u', type: 'user', text: 'question'};
  const command = {id: 'c', type: 'command', text: 'output'};
  const first = build([user, command]);
  const next = build([user, {...command, text: 'new output'}, {id: 'a', type: 'agent', text: 'answer'}]);
  assert.equal(next[0].targetAnswerId, 'a');
  assert.notEqual(next[0], first[0]);
  assert.notEqual(next[1], first[1]);
  assert.equal(first[1].items[0].text, 'output');
});

test('history cache retries an initial open failure and reopens on version change', async () => {
  let opens = 0;
  const db = {
    close() {},
    transaction() {
      return {objectStore() {return {get() {
        const request = {result: {events: ['cached']}};
        queueMicrotask(() => request.onsuccess());
        return request;
      } }; } };
    },
  };
  globalThis.indexedDB = {open() {
    opens++;
    const request = {error: new Error('temporary failure'), result: db};
    const fail = opens === 1;
    queueMicrotask(() => fail ? request.onerror() : request.onsuccess());
    return request;
  }};
  const {readHistory} = await import('../frontend/src/historyCache.js?retry-test');
  assert.equal(await readHistory('session'), null);
  assert.deepEqual(await readHistory('session'), {events: ['cached']});
  assert.equal(opens, 2);
  db.onversionchange();
  assert.deepEqual(await readHistory('session'), {events: ['cached']});
  assert.equal(opens, 3);
});

test('cache writes wait for transaction completion and absorb aborts', async () => {
  let transaction;
  let written;
  const db = {transaction() {
    transaction = {objectStore() {return {put(value, id) {written = [value, id];}};}};
    return transaction;
  }};
  globalThis.indexedDB = {open() {
    const request = {result: db};
    queueMicrotask(() => request.onsuccess());
    return request;
  }};
  const {writeHistory} = await import('../frontend/src/historyCache.js?write-test');
  let settled = false;
  const promise = writeHistory('session', {events: []}).then(() => {settled = true;});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(settled, false);
  assert.deepEqual(written, [{events: []}, 'session']);
  transaction.oncomplete();
  await promise;
  const aborted = writeHistory('session', {events: ['second']});
  await new Promise(resolve => setImmediate(resolve));
  transaction.error = new Error('quota exceeded');
  transaction.onabort();
  await aborted;
});
