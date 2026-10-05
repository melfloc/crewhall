/* crewhall Service Worker.
 *
 * It shows the notifications the page hands it and is ready for Web Push: when
 * a push subscription and VAPID keys exist on the server, `push` events arrive
 * here and are shown even if the tab is closed. There is no push backend yet
 * (see js/notify.js), so today it mainly persists/serves notifications and
 * focuses the app when one is clicked.
 */
"use strict";

self.addEventListener("install", (event) => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; }
  catch (_e) { data = { body: event.data ? event.data.text() : "" }; }
  event.waitUntil(self.registration.showNotification(data.title || "crewhall", {
    body: data.body || "",
    tag: data.tag,
    data: { url: data.url || "/" },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
    for (const client of clients) {
      if ("focus" in client) return client.focus();
    }
    return self.clients.openWindow(url);
  }));
});
