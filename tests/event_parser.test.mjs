import assert from 'node:assert/strict';
import {test} from 'node:test';
import {appendEvents, createEventParser, parseEvents} from '../frontend/src/eventParser.js';

const user = (id, text) => ({id, kind: 'user', payload: {text}});
const delta = (id, itemId, text) => ({
  id, kind: 'codex',
  payload: {method: 'item/agentMessage/delta', params: {itemId, delta: text}},
});

test('streamed results match replay and preserve previous renders', () => {
  const stream = [
    user(1, 'first question'),
    {id: 2, kind: 'codex', payload: {method: 'item/reasoning/textDelta', params: {itemId: 'r', delta: 'think'}}},
    delta(3, 'a', 'hello'), delta(4, 'a', ' world'),
    {id: 5, job_id: 'j', kind: 'job_status', payload: {status: 'completed'}},
    user(6, 'second question'),
    {id: 7, kind: 'antigravity', payload: {event: 'agy_step', step_kind: 'agent', step_id: 'b', text: 'answer'}},
    // Historical corrections must update the corresponding earlier turn.
    {id: 8, kind: 'codex', payload: {method: 'item/completed', params: {item: {id: 'a', type: 'agentMessage', text: 'corrected'}}}},
  ];
  const read = createEventParser();
  let events = [];
  let previous = read(events);
  for (const event of stream) {
    const frozen = JSON.stringify(previous);
    const firstUser = previous.items[0];
    events = appendEvents(events, [event]);
    const next = read(events);
    assert.deepEqual(next, parseEvents(events));
    assert.equal(JSON.stringify(previous), frozen);
    if (firstUser) assert.equal(next.items[0], firstUser);
    previous = next;
  }
  assert.equal(previous.items[0].text, 'first question');
  assert.equal(previous.items[3].text, 'second question');
});

test('deduplication updates when a duplicate response changes', () => {
  const read = createEventParser();
  let events = [user(1, 'question'), delta(2, 'a', 'same'), delta(3, 'b', 'same')];
  assert.equal(read(events).items.length, 2);
  events = appendEvents(events, [delta(4, 'b', ' now different')]);
  assert.equal(read(events).items.length, 3);
  assert.deepEqual(read(events), parseEvents(events));
});

test('snapshot replacement, prepended history and skipped renders reset or resume correctly', () => {
  const read = createEventParser();
  let events = [user(3, 'recent'), delta(4, 'a', 'text')];
  read(events);
  events = appendEvents(events, [delta(5, 'a', ' one')]);
  events = appendEvents(events, [delta(6, 'a', ' two')]);
  assert.deepEqual(read(events), parseEvents(events));
  const prepended = [user(1, 'old'), delta(2, 'old-response', 'old answer'), ...events];
  assert.deepEqual(read(prepended), parseEvents(prepended));
  const replacement = [user(8, 'different session'), delta(9, 'a', 'new answer')];
  assert.deepEqual(read(replacement), parseEvents(replacement));
  const changedMiddle = replacement.map((event, index) => index === 1 ? delta(9, 'a', 'replaced answer') : event);
  assert.equal(read(changedMiddle).items.at(-1).text, 'replaced answer');
});

test('append processing does not read historical event payloads again', () => {
  let reads = 0;
  const history = Array.from({length: 2000}, (_, index) => {
    const event = index % 2 === 0 ? user(index + 1, `question ${index}`) : delta(index + 1, `answer-${index}`, 'answer');
    const payload = event.payload;
    Object.defineProperty(event, 'payload', {get() { reads++; return payload; }});
    return event;
  });
  const read = createEventParser();
  const first = read(history);
  reads = 0;
  const next = read(appendEvents(history, [delta(2001, 'answer-1999', ' extended')]));
  assert.equal(reads, 0);
  assert.equal(next.items[0], first.items[0]);
  assert.equal(next.items.at(-1).text, 'answer extended');
});
