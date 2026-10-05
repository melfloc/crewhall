"use strict";
/* ---------- update status: page vs daemon version, restart pending, rollback ----------
 * This only shows and warns. Running the restart is a deliberate, agent-closing
 * action: it is never triggered from here, the page prints the command instead.
 */
async function refreshUpdateStatus(){
  let s;
  try { s = await op("update_status"); }
  catch(e){ return; }
  S.update = s;
  const badge = $("updateBadge");
  const pending = !!s.restart_pending;
  badge.style.display = pending ? "inline-flex" : "none";
  badge.classList.toggle("warn", pending);
  if(pending) badge.title = `Update installed (${s.version}); the daemon still runs ${s.daemon_version}. Restart pending.`;
}
function updateDialog(){
  const s = S.update || {};
  const row = (k, v) => el("div", {className:"ucell"}, el("div", {className:"k"}, k), el("div", {className:"v"}, v || "n/d"));
  const body = el("div", {className:"dlg-body"},
    el("h3", {}, "Updates"),
    el("div", {className:"usage-total"},
      row("Page version", s.version),
      row("Daemon version", s.daemon_version),
      row("Channel", s.channel),
      row("Rollback", s.rollback_available ? "available" : "none")),
    s.restart_pending
      ? el("p", {className:"sub", style:"margin:0"}, "An update is installed but the daemon still runs the old code. Restarting applies it — and CLOSES every running agent. Do it from a terminal with:")
      : el("p", {className:"sub", style:"margin:0"}, "The daemon and the page run the same version."),
    s.restart_pending ? el("pre", {className:"cmd", textContent:"crewhall update --restart"}) : null,
    s.rollback_available ? el("pre", {className:"cmd", textContent:"crewhall update --rollback --restart"}) : null,
    el("div", {className:"dlg-actions"},
      el("button", {className:"btn", type:"button", onclick:()=>{ const d=$("dlg"); if(d.open) d.close(); }}, "Close")));
  const d = $("dlg"); d.replaceChildren(body);
  d.onclose = null; d.onclick = e => { if(e.target === d && d.open) d.close(); };
  if(!d.open) d.showModal();
}
$("updateBadge").onclick = updateDialog;
