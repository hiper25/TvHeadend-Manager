const SHELL_CACHE = "tvheadend-manager-shell-v1.2.1";
const SHELL_ASSETS = [
  "/",
  "/app.css?v=1.2.1.0",
  "/app.js?v=1.2.1.0",
  "/favicon.svg",
  "/manifest.webmanifest",
  "/apple-touch-icon.png",
  "/pwa-icon-192.png",
  "/pwa-icon-512.png",
  "/pwa-icon-maskable-512.png"
];
const CACHEABLE_URLS = new Set(SHELL_ASSETS.map(url => new URL(url, self.location.origin).href));

self.addEventListener("install", event => {
  event.waitUntil(caches.open(SHELL_CACHE).then(cache => cache.addAll(SHELL_ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key.startsWith("tvheadend-manager-shell-") && key !== SHELL_CACHE).map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", event => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (request.mode === "navigate") {
    event.respondWith(fetch(request).catch(() => caches.match("/")));
    return;
  }

  // Dynamic API, channel icons, recordings and TvHeadend media never enter Cache Storage.
  if (!CACHEABLE_URLS.has(url.href)) return;
  event.respondWith(
    caches.match(request).then(cached => cached || fetch(request).then(response => {
      if (response.ok && response.type === "basic") {
        const copy = response.clone();
        caches.open(SHELL_CACHE).then(cache => cache.put(request, copy));
      }
      return response;
    }))
  );
});
