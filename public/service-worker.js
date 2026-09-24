// Service Worker fuer Signal Board (PWA).
// Stale-while-revalidate: liefert sofort die zwischengespeicherte Version
// (falls vorhanden, kein Warten auf das Netz), holt parallel eine frische
// Version und aktualisiert den Cache fuer den naechsten Aufruf.
const CACHE_NAME = 'signal-board-v1';
const PRECACHE_URLS = [
  '/signal-board.html',
  '/data/signals.json',
  '/fallback.json',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(PRECACHE_URLS)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  const isTracked = PRECACHE_URLS.includes(url.pathname);
  if (event.request.method !== 'GET' || !isTracked) return;

  event.respondWith(
    caches.open(CACHE_NAME).then(async (cache) => {
      const cached = await cache.match(event.request);
      const networkFetch = fetch(event.request)
        .then((res) => {
          if (res && res.ok) cache.put(event.request, res.clone());
          return res;
        })
        .catch(() => null);
      return cached || (await networkFetch) || new Response('', { status: 504 });
    })
  );
});
