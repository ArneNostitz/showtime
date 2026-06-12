const CACHE_NAME = 'yamtrack-v2';
const urlsToCache = [
  '/static/css/main.css',
  '/static/favicon/android-chrome-192x192.png',
  '/static/favicon/android-chrome-512x512.png',
  '/static/fonts/roboto-flex.woff2'
];

// Install event
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => {
        return cache.addAll(urlsToCache);
      })
  );
});

// Fetch event
self.addEventListener('fetch', (event) => {
  // Never intercept non-GET requests (POST/PUT/DELETE carry CSRF tokens or mutations)
  if (event.request.method !== 'GET') {
    return;
  }

  // Never serve navigation requests (HTML pages) from cache — they embed the CSRF
  // token, which Django rotates on login.  A stale cached page would send the old
  // token and cause a 403 on every subsequent POST.
  if (event.request.mode === 'navigate') {
    return;
  }

  event.respondWith(
    caches.match(event.request)
      .then((response) => {
        return response || fetch(event.request);
      })
  );
});

// Activate event — delete all old caches so stale HTML is evicted immediately
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames.map((cacheName) => {
          if (cacheName !== CACHE_NAME) {
            return caches.delete(cacheName);
          }
        })
      );
    })
  );
});
