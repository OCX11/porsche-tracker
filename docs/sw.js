// Rennmarkt — Service Worker v3
// Handles: caching, push notifications, notification click → open listing URL

const CACHE_NAME = 'ptox11-v3';
const ASSETS = [
  '/',
  '/index.html',
];

// ── Install ────────────────────────────────────────────────────────────────────
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(ASSETS))
  );
  self.skipWaiting();
});

// ── Activate ───────────────────────────────────────────────────────────────────
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// ── Fetch (network-first) ──────────────────────────────────────────────────────
self.addEventListener('fetch', event => {
  event.respondWith(
    fetch(event.request)
      .then(response => {
        if (response.ok && event.request.url.includes('rennmarkt.net')) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
        }
        return response;
      })
      .catch(() => caches.match(event.request))
  );
});

// ── Push received ──────────────────────────────────────────────────────────────
self.addEventListener('push', event => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = { title: 'PTOX11', body: event.data ? event.data.text() : '' };
  }

  const title   = data.title  || 'PTOX11';
  const body    = data.body   || '';
  const url     = data.url    || 'https://www.rennmarkt.net/';
  const icon    = data.icon   || '/icons/icon-192.png';
  const badge   = data.badge  || '/icons/icon-192.png';

  const options = {
    body,
    icon,
    badge,
    data: { url },
    requireInteraction: false,
    vibrate: [200, 100, 200],
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

// ── Notification click → open listing URL ─────────────────────────────────────
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url)
    ? event.notification.data.url
    : 'https://www.rennmarkt.net/';

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clientList => {
      // If a Rennmarkt tab is already open, focus it then navigate
      for (const client of clientList) {
        if (client.url.includes('rennmarkt.net') && 'focus' in client) {
          client.focus();
          client.navigate(url);
          return;
        }
      }
      // Otherwise open a new tab
      if (clients.openWindow) {
        return clients.openWindow(url);
      }
    })
  );
});
