import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../frontend/public/sw.js', import.meta.url), 'utf8');

function worker({cached = null, networkResponse = null} = {}) {
  const listeners = {};
  const writes = [];
  const cache = {
    match: async () => cached,
    put: async (...args) => writes.push(args),
  };
  vm.runInNewContext(source, {
    self: {
      location: {origin: 'https://rove.example'},
      addEventListener: (name, callback) => { listeners[name] = callback; },
    },
    caches: {open: async () => cache},
    fetch: async () => networkResponse,
    URL, Response, AbortController, setTimeout, clearTimeout,
  });
  return {
    writes,
    dispatch(path) {
      let response;
      const background = [];
      listeners.fetch({
        request: {url: `https://rove.example${path}`, method: 'GET', cache: 'default', mode: 'navigate'},
        respondWith: value => { response = Promise.resolve(value); },
        waitUntil: value => background.push(value),
      });
      return {response, background};
    },
  };
}

test('legacy verification links leave the cached app shell', async () => {
  const sw = worker({cached: new Response('cached shell')});
  const response = await sw.dispatch('/?cf_auth=1').response;
  assert.equal(response.status, 302);
  assert.equal(response.headers.get('location'), 'https://rove.example/healthz?cf_auth=1');
  assert.equal(sw.writes.length, 0);
});

test('health and Access verification requests go directly to the network', () => {
  const sw = worker({cached: new Response('cached shell')});
  for (const path of ['/healthz', '/healthz?cf_auth=1', '/cdn-cgi/access/authorized']) {
    assert.equal(sw.dispatch(path).response, undefined, path);
  }
});

test('normal navigation keeps instant cached startup', async () => {
  const sw = worker({cached: new Response('cached shell')});
  const event = sw.dispatch('/?session=existing');
  assert.equal(await (await event.response).text(), 'cached shell');
  await Promise.all(event.background);
});

test('redirected login HTML cannot replace the cached shell', async () => {
  const login = new Response('<html>Cloudflare login</html>', {headers: {'content-type': 'text/html'}});
  Object.defineProperty(login, 'redirected', {value: true});
  const sw = worker({cached: new Response('cached shell'), networkResponse: login});
  const event = sw.dispatch('/');
  await event.response;
  await Promise.all(event.background);
  assert.equal(sw.writes.length, 0);
});
