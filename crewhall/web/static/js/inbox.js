"use strict";
/* ---------- global inbox: every pending permission/question, all agents ---------- */
function inboxItems(){
  const out = [];
  (S.state?.agents || []).forEach(a => (a.interactions || []).forEach(item => out.push({agent:a, item})));
  return out;
}
function inboxRow(n){
  return el("div", {className:"inbox-row", role:"button", tabIndex:0, title:"Answer without opening the agent",
      onclick:()=>openInbox(), onkeydown:(e)=>{ if(e.key==="Enter"||e.key===" "){ e.preventDefault(); openInbox(); } }},
    ic("shield","sm"), el("span", {className:"nm"}, "Needs your answer"), el("span", {className:"badge pulse"}, n));
}
function updateInboxBadge(){
  const n = inboxItems().length;
  const btn = $("inboxBtn");
  if(btn){ btn.style.display = n ? "inline-flex" : "none"; $("inboxN").textContent = n; }
  if($("inboxDlg") && $("inboxDlg").open) renderInbox();
}
function openInbox(){
  renderInbox();
  const d = $("inboxDlg");
  if(d && !d.open) d.showModal();
}
function closeInbox(){ const d = $("inboxDlg"); if(d && d.open) d.close(); }
function renderInbox(){
  const list = $("inbox-list"); if(!list) return;
  const items = inboxItems();
  const key = items.map(x => x.item.id).join("|");
  if(list.dataset.key === key) return;
  list.dataset.key = key;
  if(!items.length){ list.replaceChildren(el("div", {className:"inbox-empty"}, ic("check","lg"), "Nothing needs your attention.")); return; }
  list.replaceChildren(...items.map(({agent, item}) => {
    const wrap = el("div", {className:"inbox-item"});
    wrap.append(el("div", {className:"inbox-from"},
      ic("terminal","sm"), el("span", {className:"nm"}, agent.name || agent.agent_id),
      el("span", {className:"chip mono"}, agent.kind), el("span", {style:"flex:1"}),
      el("button", {className:"btn sm ghost", type:"button",
        onclick:()=>{ select(agent.agent_id); closeInbox(); }}, "Open agent")));
    wrap.append(askCard(item));
    return wrap;
  }));
}
$("inboxBtn").onclick = openInbox;
$("inboxClose").onclick = closeInbox;
$("inboxDlg").addEventListener("click", e => { if(e.target === $("inboxDlg")) closeInbox(); });
