"use strict";
/* ---------- session & access: logout, connected devices, token status ---------- */
function fmtAgo(ts){ if(ts == null) return "—"; const s = Math.max(0, Math.floor(Date.now()/1000 - ts)); return s < 60 ? s + "s ago" : s < 3600 ? Math.floor(s/60) + "m ago" : Math.floor(s/3600) + "h ago"; }
async function doLogout(){
  if(!await confirmDlg({title:"Sign out?", message:"You will need the access token to sign in again.", ok:"Sign out"})) return;
  try { await fetch("/logout", {method:"POST", credentials:"same-origin"}); } catch(e){}
  location.href = "/login";
}
async function openAccess(){
  let sessions = [], token = {};
  try { sessions = (await op("web_sessions")).sessions || []; } catch(e){}
  try { token = await op("web_token_status"); } catch(e){}
  const rows = el("div", {className:"access-list"});
  if(!sessions.length) rows.append(el("div", {className:"none"}, "No other devices connected."));
  sessions.forEach(s => rows.append(el("div", {className:"access-row"},
    el("span", {className:"nm"}, s.ip || "unknown"), el("span", {className:"pm"}, s.user_agent || ""),
    el("span", {className:"pm", style:"margin-left:auto"}, "seen " + fmtAgo(s.last_seen)))));
  const body = el("div", {className:"dlg-body"},
    el("h3", {}, "Session & access"),
    el("div", {className:"usage-total"},
      el("div", {className:"ucell"}, el("div", {className:"k"}, "Token"), el("div", {className:"v"}, token.token_exists ? (token.fingerprint || "set") : "none")),
      el("div", {className:"ucell"}, el("div", {className:"k"}, "Devices"), el("div", {className:"v"}, String(sessions.length)))),
    el("h4", {className:"mc-h"}, "Connected devices"), rows,
    el("div", {className:"dlg-actions"},
      el("button", {className:"btn", type:"button", onclick:()=>{ const d=$("dlg"); if(d.open) d.close(); }}, "Close"),
      el("button", {className:"btn danger", type:"button", onclick:doLogout}, "Sign out")));
  const d = $("dlg"); d.replaceChildren(body);
  d.onclose = null; d.onclick = e => { if(e.target === d && d.open) d.close(); };
  if(!d.open) d.showModal();
}
$("logoutBtn").onclick = openAccess;
