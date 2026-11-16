/* Walking Workstation Service Worker: Instant App-Shell Boot, WebAPK, Offline Fallback & Web Push */
const CACHE_NAME = 'walking-workstation-shell-6d7f7a55a3';
const PRECACHE_ASSETS = [
  "/",
  "/manifest.webmanifest",
  "/rove-favicon.svg",
  "/rove-apple-touch.png",
  "/rove-192.png",
  "/watch/assets/InstallGuideModal-CX3_4ouY.js",
  "/watch/assets/PermissionModal-BZsqduwx.js",
  "/watch/assets/ProjectMemoriesModal-BFxJB0IC.js",
  "/watch/assets/QuotaModal-PyyOcwTW.js",
  "/watch/assets/SettingsPanel-CPvORB9x.js",
  "/watch/assets/WorktreeModal-Lf9HD3cZ.js",
  "/watch/assets/index-B8wlSjdY.js",
  "/watch/assets/index-p7kWGKM0.css",
  "/watch/assets/vendor-icons-1nB3l9_f.js",
  "/watch/assets/vendor-markdown-D6awl5rF.js",
  "/watch/assets/vendor-react-BCaoD2OB.js"
];

const OFFLINE_HTML = `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"/>
  <meta name="theme-color" content="#0d0f13"/>
  <meta http-equiv="refresh" content="8"/>
  <title>Rove · 网络重连中</title>
  <style>
    :root { color-scheme: dark; }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100dvh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
      background: #0d0f13;
      color: #f3f4f6;
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", sans-serif;
    }
    .card {
      max-width: 400px;
      width: 100%;
      padding: 28px 24px;
      background: #151922;
      border: 1px solid rgba(255, 255, 255, 0.1);
      border-radius: 20px;
      text-align: center;
      box-shadow: 0 20px 50px rgba(0, 0, 0, 0.45);
    }
    .badge {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 56px;
      height: 56px;
      border-radius: 16px;
      background: linear-gradient(135deg, #1b2538, #101521);
      border: 1px solid rgba(148, 163, 184, 0.25);
      margin-bottom: 16px;
      font-weight: 700;
      font-size: 20px;
      color: #38bdf8;
    }
    h1 { margin: 0 0 10px; font-size: 18px; font-weight: 600; }
    p { margin: 0 0 18px; font-size: 13.5px; line-height: 1.65; color: #9ca3af; }
    .actions { display: flex; flex-direction: column; gap: 10px; }
    a.btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 100%;
      height: 42px;
      border-radius: 12px;
      background: #f8fafc;
      color: #0f172a;
      font-size: 14px;
      font-weight: 600;
      text-decoration: none;
    }
    a.btn.secondary {
      background: rgba(255, 255, 255, 0.08);
      color: #e5e7eb;
      font-weight: 500;
    }
    small { display: block; margin-top: 14px; font-size: 12px; color: #6b7280; }
  </style>
</head>
<body>
  <main class="card">
    <div class="badge">R</div>
    <h1>暂时无法连接 Mac mini</h1>
    <p>移动网络可能超时或代理未覆盖当前桌面应用。Mac mini 上的 Codex、Antigravity 与终端仍在后台持续运行。</p>
    <div class="actions">
      <a class="btn" href="/">立即重连工作台</a>
      <a class="btn secondary" href="/healthz?cf_auth=1">重新验证 Cloudflare Access</a>
    </div>
    <small>每 8 秒自动尝试恢复连接…</small>
  </main>
</body>
</html>`;

function fetchWithTimeout(input, timeoutMs = 4000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  return fetch(input, {signal: controller.signal, credentials: 'same-origin'}).finally(() => {
    clearTimeout(timer);
  });
}

async function cacheValidResponse(cache, key, res, isScriptOrStyle = false) {
  if (!res || !res.ok) return;
  const ct = res.headers.get('content-type') || '';
  if (isScriptOrStyle && ct.includes('text/html')) return;
  if (isScriptOrStyle) {
    if (!/javascript|text\/css/.test(ct)) return;
    const bytes = (await res.clone().arrayBuffer()).byteLength;
    const expected = Number(res.headers.get('x-rove-asset-bytes'));
    if (!bytes || (expected > 0 && expected !== bytes)) return;
  }
  await cache.put(key, res).catch(() => {});
}

function isBundleAsset(pathname) {
  return pathname.startsWith('/assets/') || pathname.startsWith('/watch/assets/');
}

async function sanitizeHtmlForCache(response) {
  try {
    const text = await response.text();
    const sanitized = text.replace(
      /<script>\s*window\.__ROVE_BOOTSTRAP__\s*=[\s\S]*?<\/script>/g,
      '<!--__ROVE_BOOTSTRAP__-->'
    );
    return new Response(sanitized, {
      status: response.status,
      statusText: response.statusText,
      headers: response.headers,
    });
  } catch {
    return response;
  }
}

self.addEventListener('install', event => {
  self.skipWaiting();
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    await Promise.allSettled(
      PRECACHE_ASSETS.map(async assetUrl => {
        try {
          if (await cache.match(assetUrl)) return;
          const res = await fetchWithTimeout(assetUrl, 8000);
          const isBundle = isBundleAsset(assetUrl);
          if (assetUrl === '/' || assetUrl === '/index.html') {
            const ct = res.headers.get('content-type') || '';
            if (res.ok && ct.includes('text/html')) {
              const cleanRes = await sanitizeHtmlForCache(res);
              await cache.put('/', cleanRes.clone());
              await cache.put('/index.html', cleanRes);
            }
          } else {
            await cacheValidResponse(cache, assetUrl, res, isBundle);
          }
        } catch {}
      }),
    );
  })());
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    if (self.registration.navigationPreload) {
      await self.registration.navigationPreload.disable().catch(() => {});
    }
    const keys = await caches.keys();
    await Promise.all(
      keys
        .filter(k => (k.startsWith('walking-workstation-') || k === 'walking-workstation-shell-6d7f7a55a3') && k !== CACHE_NAME)
        .map(k => caches.delete(k)),
    );
    await self.clients.claim();
  })());
});

self.addEventListener('message', event => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
  if (event.data && event.data.type === 'PURGE_CACHE') {
    event.waitUntil(
      caches.keys().then(keys => Promise.all(keys.map(k => caches.delete(k))))
    );
  }
});

function isStaticCacheableAsset(pathname) {
  if (isBundleAsset(pathname)) return true;
  return (
    pathname === '/favicon.svg' ||
    pathname === '/favicon.ico' ||
    pathname === '/rove-favicon.svg' ||
    pathname === '/apple-touch-icon.png' ||
    pathname === '/apple-touch-icon-precomposed.png' ||
    pathname === '/rove-apple-touch.png' ||
    pathname === '/pwa-192.png' ||
    pathname === '/rove-192.png' ||
    pathname === '/pwa-512.png' ||
    pathname === '/rove-512.png' ||
    pathname === '/pwa-maskable-512.png' ||
    pathname === '/rove-maskable-512.png' ||
    pathname === '/manifest.webmanifest'
  );
}

self.addEventListener('fetch', event => {
  const {request} = event;
  if (request.method !== 'GET') return;
  if (request.cache === 'reload' || request.cache === 'no-store') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Older cached shells still link here. Route them through the uncached health path.
  if (url.pathname === '/' && url.searchParams.has('cf_auth')) {
    event.respondWith(Response.redirect(new URL('/healthz?cf_auth=1', self.location.origin).href, 302));
    return;
  }

  // Strictly bypass API routes, WebSockets, Cloudflare Access endpoints, and health checks.
  if (
    url.pathname.startsWith('/api/') ||
    url.pathname.startsWith('/cdn-cgi/') ||
    url.pathname === '/sw.js' ||
    url.pathname === '/healthz'
  ) {
    return;
  }

  // Navigation requests: Instant App-Shell (<10ms boot from current version cache) + non-blocking background revalidation.
  if (request.mode === 'navigate') {
    event.respondWith((async () => {
      const cache = await caches.open(CACHE_NAME);
      const cachedShell = (await cache.match('/')) || (await cache.match('/index.html'));

      // 1. Instant Cache-First Return: If the current version's self-contained App Shell is cached, serve it IMMEDIATELY (0ms network delay).
      if (cachedShell) {
        event.waitUntil((async () => {
          try {
            const netRes = await fetchWithTimeout('/', 10000);
            const ct = netRes?.headers?.get('content-type') || '';
            if (netRes && netRes.ok && !netRes.redirected && ct.includes('text/html')) {
              const cleanRes = await sanitizeHtmlForCache(netRes.clone());
              await cache.put('/', cleanRes.clone());
              await cache.put('/index.html', cleanRes);
            }
          } catch {}
        })());
        return cachedShell;
      }

      // 2. Cold Cache Fallback (first launch or new build): Network fetch with generous timeout for throttled networks
      try {
        const netRes = await fetchWithTimeout('/', 20000);
        const ct = netRes?.headers?.get('content-type') || '';
        if (netRes && netRes.ok && !netRes.redirected && ct.includes('text/html')) {
          const cleanRes = await sanitizeHtmlForCache(netRes.clone());
          await cache.put('/', cleanRes.clone());
          await cache.put('/index.html', cleanRes);
          return netRes;
        }
      } catch {}

      return new Response(OFFLINE_HTML, {
        status: 503,
        headers: {'Content-Type': 'text/html; charset=utf-8'},
      });
    })());
    return;
  }

  // Content-hashed bundles & static icons: Resilient Cache-First with network fallback.
  if (isStaticCacheableAsset(url.pathname)) {
    event.respondWith((async () => {
      const isBundle = isBundleAsset(url.pathname);
      const hit = (await caches.match(request)) || (await caches.match(url.pathname));
      if (hit && isBundle) {
        const bytes = (await hit.clone().arrayBuffer()).byteLength;
        const expected = Number(hit.headers.get('x-rove-asset-bytes'));
        if (bytes && (!expected || expected === bytes)) return hit;
        await (await caches.open(CACHE_NAME)).delete(request);
      } else if (hit && !isBundle) {
        return hit;
      }

      try {
        const netRes = await fetchWithTimeout(request, 25000);
        if (netRes && netRes.ok) {
          const cache = await caches.open(CACHE_NAME);
          await cacheValidResponse(cache, request, netRes.clone(), isBundle);
        }
        return netRes;
      } catch (err) {
        if (hit && (await hit.clone().arrayBuffer()).byteLength) return hit;
        const altHit = await caches.match(url.pathname);
        if (altHit && (await altHit.clone().arrayBuffer()).byteLength) return altHit;
        throw err;
      }
    })());
    return;
  }
});

self.addEventListener('push', event => {
  let payload = {
    title: 'Rove',
    body: '后台任务有新的状态更新，点击查看。',
    tag: 'workstation-push',
    url: '/',
    sessionId: null,
    kind: 'info',
    requireInteraction: false,
  };

  if (event.data) {
    try {
      payload = {...payload, ...event.data.json()};
    } catch {
      payload.body = event.data.text() || payload.body;
    }
  }

  event.waitUntil((async () => {
    if (self.navigator && typeof self.navigator.setAppBadge === 'function' && payload.kind === 'approval') {
      self.navigator.setAppBadge(1).catch(() => {});
    }
    await self.registration.showNotification(payload.title || 'Rove', {
      body: payload.body || '',
      icon: '/pwa-192.png',
      badge: '/pwa-192.png',
      tag: payload.tag || 'workstation-push',
      renotify: true,
      requireInteraction: Boolean(payload.requireInteraction),
      data: {
        url: payload.url || (payload.sessionId ? `/?session=${encodeURIComponent(payload.sessionId)}` : '/'),
        sessionId: payload.sessionId || null,
        kind: payload.kind || 'info',
      },
    });
  })());
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const data = event.notification.data || {};
  const sessionId = data.sessionId || null;
  const relUrl = data.url || (sessionId ? `/?session=${encodeURIComponent(sessionId)}` : '/');
  const targetUrl = new URL(relUrl, self.registration.scope).href;

  event.waitUntil((async () => {
    if (self.navigator && typeof self.navigator.clearAppBadge === 'function') {
      self.navigator.clearAppBadge().catch(() => {});
    }
    const windowClients = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
    for (const client of windowClients) {
      if (new URL(client.url).origin === self.location.origin) {
        client.postMessage({
          type: 'WORKSTATION_OPEN_TARGET',
          sessionId,
          url: targetUrl,
        });
        if ('focus' in client) {
          await client.focus();
        }
        return;
      }
    }
    if (self.clients.openWindow) {
      await self.clients.openWindow(targetUrl);
    }
  })());
});
