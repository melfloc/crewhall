"use strict";
/* ---------- browser notifications ----------
 * Web Push (delivering a notification while the tab is closed) needs a push
 * subscription and VAPID keys on the server; crewhall has no push backend
 * yet, so this ships the in-page Notifications API plus a Service Worker that is
 * ready to receive `push` events when a backend exists. See sw.js.
 */
function notificationsSupported(){
  return typeof Notification !== "undefined" && "serviceWorker" in navigator;
}
function notificationsOn(){
  return notificationsSupported() && Notification.permission === "granted" && store.get("at.notify") === "1";
}
function renderNotifyBtn(){
  const b = $("notifyBtn"); if(!b) return;
  if(!notificationsSupported()){ b.style.display = "none"; return; }
  b.style.display = "inline-flex";
  const on = notificationsOn();
  b.classList.toggle("on", on);
  b.setAttribute("aria-pressed", on ? "true" : "false");
  b.title = on ? "Notifications on — click to turn off"
    : Notification.permission === "denied" ? "Notifications blocked in your browser settings"
    : "Enable browser notifications";
}
async function toggleNotifications(){
  if(Notification.permission === "denied"){ toast("Notifications are blocked in your browser settings", "err"); return; }
  if(Notification.permission !== "granted"){
    let p = "denied";
    try { p = await Notification.requestPermission(); } catch(e){}
    if(p !== "granted"){ toast("Notifications not enabled", "info"); return; }
  }
  const on = store.get("at.notify") === "1";
  store.set("at.notify", on ? "0" : "1");
  renderNotifyBtn();
  toast(on ? "Notifications off" : "Notifications on", on ? "info" : "ok");
}
async function notify(title, body, opts = {}){
  if(!notificationsOn()) return;
  const options = {body, tag:opts.tag, renotify:false, data:{url:opts.url || "/"}};
  try {
    if(navigator.serviceWorker){
      const reg = await Promise.race([navigator.serviceWorker.ready, new Promise(r => setTimeout(() => r(null), 1500))]);
      if(reg){ await reg.showNotification(title, options); return; }
    }
  } catch(e){}
  try { new Notification(title, options); } catch(e){}
}
$("notifyBtn").onclick = toggleNotifications;
if(notificationsSupported()){
  navigator.serviceWorker.register("/sw.js").catch(()=>{});
}
renderNotifyBtn();
