/* Curbside, installed.
 *
 * The one rule here: a listing page must never be served from a cache. Half of
 * what this bot shows is gone within the hour, and a stale card that says a
 * free sofa is still on the kerb sends someone across town for nothing. That is
 * worse than an error message, so pages are network-only with an offline
 * notice, and no HTML is ever stored.
 *
 * What IS cached is the part that cannot go stale: the icons, and the photos
 * under /thumb/, which are content-addressed by listing id and rewritten only
 * when the listing itself is refetched.
 */
const VERSION = 'curbside-v1';
const SHELL = VERSION + '-shell';
const MEDIA = VERSION + '-media';
const OFFLINE = '/static/offline.html';
const MEDIA_MAX = 400;                       // roughly a month of browsing

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL)
      .then((c) => c.addAll([OFFLINE, '/static/icon-192.png']))
      .then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => !k.startsWith(VERSION)).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()));
});

async function trim(cache) {
  const keys = await cache.keys();
  if (keys.length <= MEDIA_MAX) return;
  for (const k of keys.slice(0, keys.length - MEDIA_MAX)) await cache.delete(k);
}

async function fromCacheFirst(request) {
  const cache = await caches.open(MEDIA);
  const hit = await cache.match(request);
  if (hit) return hit;
  const res = await fetch(request);
  // `basic` only: a /thumb/ with no local copy redirects to the source, and an
  // opaque cross-origin response is not worth a cache slot.
  if (res.ok && res.type === 'basic') {
    cache.put(request, res.clone());
    trim(cache);
  }
  return res;
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;              // triage posts, untouched
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (url.pathname.startsWith('/thumb/') || url.pathname.startsWith('/static/')) {
    event.respondWith(fromCacheFirst(request));
    return;
  }

  event.respondWith(
    fetch(request).catch(async () => (await caches.match(OFFLINE))
      || new Response('Offline', {status: 503, headers: {'Content-Type': 'text/plain'}})));
});
