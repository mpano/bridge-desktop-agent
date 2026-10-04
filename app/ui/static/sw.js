"use strict";

// Bridge's service worker on the phone: shows pushed notifications (the Mac encrypts them;
// only this phone can read them) and opens the right screen when one is tapped.
const VIEWS = ["today", "chat", "inbox"];

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = {body: event.data ? event.data.text() : ""};
  }
  const view = VIEWS.includes(data.view) ? data.view : "today";
  event.waitUntil(self.registration.showNotification(String(data.title || "Bridge").slice(0, 100), {
    body: String(data.body || "").slice(0, 400),
    icon: "/ui/icon-192.png",
    data: {view},
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const view = VIEWS.includes((event.notification.data || {}).view) ? event.notification.data.view : "today";
  event.waitUntil((async () => {
    const open = await self.clients.matchAll({type: "window", includeUncontrolled: true});
    for (const client of open) {
      client.postMessage({view});
      return client.focus();
    }
    return self.clients.openWindow(`/#view=${view}`);
  })());
});
