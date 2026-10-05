"use strict";
/* ---------- mission control: one card per agent + the message flow ---------- */
function ago(ts){
  if(ts == null) return "—";
  const s = Math.max(0, Math.floor(Date.now()/1000 - ts));
  if(s < 5) return "just now";
  if(s < 60) return s + "s ago";
  if(s < 3600) return Math.floor(s/60) + "m ago";
  if(s < 86400) return Math.floor(s/3600) + "h ago";
  return Math.floor(s/86400) + "d ago";
}
function doingNow(a){
  if(a.activity && a.activity.kind && !["idle","unknown"].includes(a.activity.kind)) return actText(a);
  if((a.interactions||[]).length) return "Waiting for your answer";
  switch(a.state){
    case "working": return "Working now";
    case "waiting_input": return "Waiting for your input";
    case "starting": return "Starting up";
    case "ready": return "Idle, ready";
    case "exited": return "Exited";
    case "error": return "Stopped with an error";
    default: return "Unknown";
  }
}
function agentCard(a, msgs){
  const sent = msgs.filter(m => m.sender === a.agent_id).length;
  const recv = msgs.filter(m => m.recipient === a.agent_id).length;
  const n = (a.interactions||[]).length;
  const card = el("div", {className:"mc-card " + (a.state||"unknown")});
  card.append(el("div", {className:"mc-head"},
    el("span", {className:`avatar ${a.state||"unknown"}`, style:`--h:${hue(a.kind||a.name)}`}, (a.name||a.agent_id||"?").trim().charAt(0) || "?"),
    el("div", {style:"min-width:0;flex:1"}, el("div", {className:"nm"}, a.name||a.agent_id),
      el("div", {className:"pm", style:"margin:0"}, a.kind)),
    pill(a)));
  card.append(el("div", {className:"mc-doing"}, ic("activity","sm"), doingNow(a)));
  card.append(el("div", {className:"mc-stats"},
    el("span", {className:"mc-stat", title:"Last output/input"}, "active ", ago(a.last_output_at || a.last_input_at || a.created_at)),
    el("span", {className:"mc-stat"}, `${sent} sent`),
    el("span", {className:"mc-stat"}, `${recv} received`),
    n ? el("span", {className:"mc-stat attn", title:"Pending permissions/questions"}, `${n} pending`) : null));
  if(a.evidence) card.append(el("div", {className:"mc-ev", title:a.evidence}, a.evidence));
  return card;
}
function missionTimeline(msgs){
  const wrap = el("div", {className:"mc-timeline"});
  if(!msgs.length){ wrap.append(el("div", {className:"none"}, "(no messages yet)")); return wrap; }
  const marks = {acknowledged:["✓✓","ok"], injected:["✓","ok"], queued:["…","pend"], failed:["✗","bad"]};
  msgs.slice(-30).reverse().forEach(m => {
    const [mark, cls] = marks[m.status] || (m.delivered ? ["✓","ok"] : ["✗","bad"]);
    wrap.append(el("div", {className:"mc-msg"},
      el("span", {className:"mk " + cls}, mark),
      el("span", {className:"who"}, `${m.sender_name||m.sender} → ${m.recipient_name||m.recipient}`),
      el("span", {className:"body", title:m.body}, m.body),
      el("span", {className:"t"}, ago(m.timestamp))));
  });
  return wrap;
}
/* Scope: null = every agent, or a team id = its members and the messages they exchanged. */
function missionScope(){
  const all = S.state?.agents || [], msgs = S.state?.messages || [];
  const team = S.missionTeam ? teamById(S.missionTeam) : null;
  if(!team){ S.missionTeam = null; return {team:null, agents:all, msgs}; }
  const ids = new Set((team.members||[]).map(m => m.agent_id));
  return {team, agents:all.filter(x => ids.has(x.agent_id)),
          msgs:msgs.filter(m => ids.has(m.sender) || ids.has(m.recipient))};
}
function missionSummary(agents){
  const c = (f) => agents.filter(f).length;
  const parts = [[c(x => x.state === "working"), "working"], [c(needsYou), "need you"],
                 [c(x => (x.activity||{}).kind === "shell" || (x.activity||{}).kind === "background"), "running commands"],
                 [c(x => x.state === "exited" || x.state === "error"), "stopped"]].filter(([n]) => n);
  return el("div", {className:"mc-sum"}, parts.length ? parts.map(([n, l]) => el("span", {className:"chip"}, `${n} ${l}`)) : el("span", {className:"muted"}, "All quiet"));
}
function requestRows(ids){
  const reqs = (S.state?.requests || []).filter(r => !ids || (ids.has(r.sender) && ids.has(r.recipient)));
  if(!reqs.length) return null;
  const list = el("div", {className:"mc-reqs"});
  reqs.forEach(r => list.append(el("div", {className:"mc-req"},
    el("span", {className:"mono"}, r.request_id),
    el("span", {className:"who"}, `${r.sender} → ${r.recipient}`),
    el("span", {className:"st"}, r.state),
    el("span", {className:"t"}, ago(r.created_at)),
    el("button", {className:"btn sm", type:"button", onclick:async()=>{
      try { await op("request_cancel", {request_id:r.request_id}); toast("Request cancelled", "ok"); }
      catch(e){ flash(e.message); } }}, "Cancel"))));
  return el("div", {}, el("h4", {className:"mc-h"}, "Open requests", el("span", {className:"chip"}, reqs.length)), list);
}
function renderMission(){
  const body = $("mission-body"); if(!body) return;
  const {team, agents, msgs} = missionScope(), teams = S.state?.teams || [];
  const ids = team ? new Set((team.members||[]).map(m => m.agent_id)) : null;
  const sel = el("select", {id:"mission-scope", className:"input", "aria-label":"Mission control scope",
    onchange:(e)=>{ S.missionTeam = e.target.value || null; renderMission(); }},
    el("option", {value:""}, "All agents"), ...teams.map(t => el("option", {value:t.team_id, selected:t.team_id === S.missionTeam}, `Team: ${t.name}`)));
  setKids(body,
    el("div", {className:"mc-scope"}, sel, team && team.workspace ? el("span", {className:"muted", title:team.workspace}, ic("folder","sm"), " ", team.workspace) : null),
    missionSummary(agents),
    el("h4", {className:"mc-h"}, team ? team.name : "Agents", el("span", {className:"chip"}, agents.length)),
    (() => { const grid = el("div", {className:"mc-grid"});
      if(!agents.length) grid.append(el("div", {className:"none"}, team ? "(no members yet)" : "(no agents)"));
      agents.forEach(a => grid.append(agentCard(a, msgs))); return grid; })(),
    requestRows(ids),
    el("h4", {className:"mc-h"}, "Message flow", el("span", {className:"chip"}, msgs.length)),
    missionTimeline(msgs));
}
function renderMissionIfOpen(){ if($("missionDlg") && $("missionDlg").open) renderMission(); }
function openMission(teamId){ if(teamId !== undefined) S.missionTeam = teamId || null; renderMission(); const d = $("missionDlg"); if(d && !d.open) d.showModal(); }
function closeMission(){ const d = $("missionDlg"); if(d && d.open) d.close(); }
$("missionBtn").onclick = ()=>openMission();
$("missionClose").onclick = closeMission;
$("missionDlg").addEventListener("click", e => { if(e.target === $("missionDlg")) closeMission(); });
