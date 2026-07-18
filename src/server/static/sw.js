// toolwatch 서비스 워커 (E3) — 웹 푸시 수신·표시 최소 구현
self.addEventListener("push", function (event) {
  var data = { title: "toolwatch", body: "" };
  if (event.data) {
    try { data = event.data.json(); } catch (e) { data.body = event.data.text(); }
  }
  event.waitUntil(self.registration.showNotification(data.title, { body: data.body }));
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  event.waitUntil(clients.openWindow("/me/loans"));
});
