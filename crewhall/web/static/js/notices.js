"use strict";
/* ---------- notices: changes crewhall handled on its own ----------
 * Right now this explains an automatic agent-CLI switch: when OpenCode (or
 * another provider) updates past the range its adapter was verified against,
 * crewhall switches to a compatible install by itself and records a notice.
 * The modal says what happened and that no action is needed; dismissal is
 * persisted server-side so it is shown once.
 */
let _noticesOpen = false;

async function refreshNotices(){
  if(_noticesOpen) return;
  const dlg = $("dlg");
  if(dlg && dlg.open) return;  // never steal an open dialog; retry next tick
  let r;
  try { r = await op("provider_notices"); } catch(e){ return; }
  const unseen = (r.notices || []).filter(n => !n.acked);
  if(!unseen.length) return;
  showNoticeModal(unseen);
}

function showNoticeModal(items){
  const dlg = $("dlg");
  if(!dlg) return;
  _noticesOpen = true;
  const icon = l => l === "err" ? "alert" : l === "ok" ? "check" : "msg";
  const body = el("div", {className:"dlg-body"},
    el("h3", {}, items.length > 1 ? `crewhall · ${items.length} notices` : items[0].title),
    ...items.map(n => el("div", {className:"field"},
      el("div", {className:"nm"}, ic(icon(n.level), "sm"), " ", n.title),
      el("p", {className:"sub", style:"margin:0"}, n.body))),
    el("div", {className:"dlg-actions"},
      el("button", {className:"btn primary", type:"button", onclick:async()=>{
        try { await op("notice_ack"); } catch(e){}
        if(dlg.open) dlg.close();
        _noticesOpen = false;
      }}, "Got it")));
  dlg.replaceChildren(body);
  dlg.onclose = () => { _noticesOpen = false; };
  dlg.onclick = e => { if(e.target === dlg && dlg.open) dlg.close(); };
  if(!dlg.open) dlg.showModal();
  // Best-effort desktop/browser notification too (the modal may be backgrounded).
  try { if(typeof notify === "function") notify(items[0].title, items[0].body, {tag:"crewhall-notice"}); } catch(e){}
}
