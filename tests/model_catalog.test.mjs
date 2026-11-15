import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../frontend/src/App.jsx', import.meta.url), 'utf8');
const start = source.indexOf('function hasSignificantStateChange(');
const end = source.indexOf('\nfunction loadBootstrap(', start);
const changed = vm.runInNewContext(`(${source.slice(start, end)})`);

test('catalog-only updates refresh the model picker', () => {
  const state = {projects: [], sessions: [], jobs: [], catalog: {
    antigravity: {source: 'live', updated: 1, models: [{id: 'existing'}]},
  }};
  assert.equal(changed(state, JSON.parse(JSON.stringify(state))), false);
  const next = structuredClone(state);
  next.catalog.antigravity.models.push({id: 'new-model'});
  assert.equal(changed(state, next), true);
});

test('model metadata and capability updates also reach the UI', () => {
  const state = {projects: [], sessions: [], jobs: [], catalog: {
    antigravity: {models: [{id: 'model-xhigh', defaultEffort: 'medium'}]},
  }};
  const next = structuredClone(state);
  next.catalog.antigravity.models[0].defaultEffort = 'xhigh';
  assert.equal(changed(state, next), true);
});

test('selecting an Antigravity tier applies its default effort', () => {
  const start = source.indexOf('  function handleModelChange(');
  const end = source.indexOf('\n  function handleEffortChange(', start);
  const preferences = {};
  const choose = vm.runInNewContext(`(${source.slice(start, end)})`, {
    engine: 'antigravity', current: null, effort: 'medium', fast: false,
    catalog: {antigravity: {models: [{id: 'model-xhigh', defaultEffort: 'xhigh', efforts: ['medium', 'xhigh']}]}},
    setModel: value => { preferences.model = value; },
    setEffort: value => { preferences.effort = value; },
    setFast: () => {},
    persistSessionPrefs: () => {},
  });
  choose('model-xhigh');
  assert.deepEqual(preferences, {model: 'model-xhigh', effort: 'xhigh'});
});
