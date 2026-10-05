"use strict";
/* ---------- archived agents: read-only record of agents that were closed ---------- */
function fmtDate(ts){ return ts ? new Date(ts * 1000).toLocaleString() : "—"; }
async function openArchived(){
  let r;
  try { r = await op("agent_archive_list"); } catch(e){ flash(e.message); return; }
  const list = el("div", {className:"arch-list"});
  if(!r.archived.length) list.append(el("div", {className:"none"}, "No agents have been closed yet."));
  r.archived.forEach(a => list.append(el("div", {className:"arch-row", role:"button", tabIndex:0,
      onclick:()=>showArchived(a.agent_id), onkeydown:(e)=>{ if(e.key==="Enter") showArchived(a.agent_id); }},
    ic("terminal", "sm"),
    el("div", {style:"min-width:0;flex:1"},
      el("div", {className:"nm"}, a.name || a.agent_id),
      el("div", {className:"pm"}, `${a.kind} · ${(a.summary||{}).messages || 0} messages · closed ${fmtDate(a.archived_at)}`)))));
  const body = el("div", {className:"dlg-body"},
    el("h3", {}, "Archived agents"),
    el("p", {className:"sub", style:"margin:0"}, "A summary of agents that were closed. They are not running and are not restored from here."),
    list,
    el("div", {className:"dlg-actions"},
      el("button", {className:"btn", type:"button", onclick:()=>{ const d=$("dlg"); if(d.open) d.close(); }}, "Close")));
  showNode(body);
}
async function showArchived(id){
  let r;
  try { r = await op("agent_archive_get", {target:id}); } catch(e){ flash(e.message); return; }
  if(!r.found){ toast("Archive not found", "info"); return; }
  const a = r.archive;
  const msgs = el("div", {className:"arch-msgs"});
  (a.messages || []).slice().reverse().forEach(m => msgs.append(el("div", {className:"arch-msg"},
    el("span", {className:"who"}, `${m.sender_name||m.sender} → ${m.recipient_name||m.recipient}`),
    el("span", {className:"body", title:m.body}, m.body))));
  const body = el("div", {className:"dlg-body"},
    el("h3", {}, a.name || a.agent_id),
    el("div", {className:"usage-total"},
      el("div", {className:"ucell"}, el("div", {className:"k"}, "Kind"), el("div", {className:"v"}, a.kind)),
      el("div", {className:"ucell"}, el("div", {className:"k"}, "Closed"), el("div", {className:"v"}, fmtDate(a.archived_at))),
      el("div", {className:"ucell"}, el("div", {className:"k"}, "Teams"), el("div", {className:"v"}, (a.teams||[]).join(", ") || "—"))),
    a.cwd ? el("pre", {className:"cmd", textContent:a.cwd}) : null,
    el("h4", {className:"mc-h"}, "Message summary"), msgs,
    el("div", {className:"dlg-actions"},
      el("button", {className:"btn", type:"button", onclick:openArchived}, "Back"),
      el("button", {className:"btn", type:"button", onclick:()=>{ const d=$("dlg"); if(d.open) d.close(); }}, "Close")));
  showNode(body);
}
function showNode(node){ const d = $("dlg"); d.replaceChildren(node); d.onclose = null; d.onclick = e => { if(e.target === d) d.close(); }; if(!d.open) d.showModal(); }
