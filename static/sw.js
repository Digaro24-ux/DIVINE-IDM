// Minimal service worker: makes the app installable. Downloads always need the live server, so nothing is cached.
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
