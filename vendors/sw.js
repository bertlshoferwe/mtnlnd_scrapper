// Vendors' service worker: keeps the app shell (the page, its manifest and icons) so the installed app
// opens without a connection and says it's offline. No data is cached: /api/ always goes to the network.
var SHELL = "vendors-shell-v1";
var FILES = ["/vendors/", "/vendors/manifest.webmanifest", "/vendors/icons/icon.svg",
             "/vendors/icons/icon-180.png", "/vendors/icons/icon-192.png", "/vendors/icons/icon-512.png"];

self.addEventListener("install", function (e) {
  e.waitUntil(caches.open(SHELL).then(function (c) { return c.addAll(FILES); }).then(function () { return self.skipWaiting(); }));
});

self.addEventListener("activate", function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k !== SHELL; }).map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});

self.addEventListener("fetch", function (e) {
  var req = e.request, url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin || !url.pathname.startsWith("/vendors/")) return;
  if (req.mode === "navigate") {
    // the newest page when online (and kept for next time), the kept one when not
    e.respondWith(fetch(req).then(function (r) {
      var copy = r.clone();
      caches.open(SHELL).then(function (c) { c.put("/vendors/", copy); });
      return r;
    }).catch(function () { return caches.match("/vendors/"); }));
    return;
  }
  if (FILES.indexOf(url.pathname) >= 0) {
    e.respondWith(caches.match(url.pathname).then(function (hit) { return hit || fetch(req); }));
  }
});
