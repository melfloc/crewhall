"use strict";
function agentById(id){ return (S.state?.agents||[]).find(a=>a.agent_id===id); }
function teamById(id){ return (S.state?.teams||[]).find(t=>t.team_id===id); }
function terminalById(id){ return (S.terminals||[]).find(t=>t.session_id===id); }

/* ---------- drag & drop: reorder teams, move agents between them ---------- */
let DRAG = null;
function teamOrderIds(){ try{ return JSON.parse(store.get("at.teamOrder")||"[]"); }catch(e){ return []; } }
function orderTeams(teams){
  const idx = new Map(teamOrderIds().map((id,i)=>[id,i]));
  const rank = t => idx.has(t.team_id) ? idx.get(t.team_id) : 1e9;
  return [...teams].sort((a,b)=> rank(a) - rank(b));
}
function agentOrderIds(){ try{ return JSON.parse(store.get("at.agentOrder")||"[]"); }catch(e){ return []; } }
function dragStart(e, kind, id){
  DRAG = {kind, id};
  try{ e.dataTransfer.setData("text/plain", kind + ":" + id); e.dataTransfer.effectAllowed = "move"; }catch(_){}
  e.currentTarget.classList.add("dragging");
}
function dragEnd(e){
  e.currentTarget.classList.remove("dragging");
  document.querySelectorAll(".drop-target").forEach(n=>n.classList.remove("drop-target"));
  DRAG = null;
}
function dragOver(e, node){ if(DRAG){ e.preventDefault(); node.classList.add("drop-target"); } }
function dropTeam(e, teamId, node){
  e.preventDefault(); node.classList.remove("drop-target");
  if(!DRAG) return;
  if(DRAG.kind === "team") reorderTeam(DRAG.id, teamId);
  else if(DRAG.kind === "agent") moveAgentToTeam(DRAG.id, teamId);
}
function dropUngrouped(e, node){
  e.preventDefault(); node.classList.remove("drop-target");
  if(DRAG && DRAG.kind === "agent") moveAgentToTeam(DRAG.id, null);
}
function dropAgent(e, beforeId, node){
  e.preventDefault(); node.classList.remove("drop-target");
  if(DRAG && DRAG.kind === "agent" && DRAG.id !== beforeId){
    const ids = agentOrderIds().filter(x => x !== DRAG.id);
    let to = ids.indexOf(beforeId); if(to < 0) to = ids.length;
    ids.splice(to, 0, DRAG.id);
    store.set("at.agentOrder", JSON.stringify(ids)); render();
  }
}
function reorderTeam(src, before){
  const ids = orderTeams(S.state.teams||[]).map(t => t.team_id).filter(id => id !== src);
  let to = ids.indexOf(before); if(to < 0) to = ids.length;
  ids.splice(to, 0, src);
  store.set("at.teamOrder", JSON.stringify(ids)); render();
}
async function moveAgentToTeam(agentId, teamId){
  const current = (S.state.teams||[]).filter(t => (t.members||[]).some(m => m.agent_id === agentId));
  for(const t of current){
    if(t.team_id !== teamId){
      try{ await op("team_remove_member", {target:t.team_id, agent:agentId}); }
      catch(e){ flash(e.message); }
    }
  }
  if(teamId && !current.some(t => t.team_id === teamId)){
    try{ await op("team_add_member", {target:teamId, agent:agentId}); toast("Moved to team", "ok", 1800); }
    catch(e){ flash(e.message); }
  }
  render();
}
const needsYou = a => a.state === "waiting_input" || (a.interactions||[]).length > 0;

/* ---------- sidebar ---------- */
function matches(a){
  if(S.filter === "working" && a.state !== "working") return false;
  if(S.filter === "attn" && !needsYou(a)) return false;
  const q = S.q.trim().toLowerCase();
  return !q || [a.name, a.agent_id, a.kind, a.cwd, a.state].some(v => String(v||"").toLowerCase().includes(q));
}
function renderFilters(agents){
  const counts = { all:agents.length, working:agents.filter(a=>a.state==="working").length, attn:agents.filter(needsYou).length };
  $("filters").replaceChildren(...[["all","All"],["working","Working"],["attn","Needs you"]].map(([k, label]) =>
    el("button", {className:S.filter===k ? "on" : "", type:"button", "aria-pressed":S.filter===k,
      onclick:()=>{ S.filter = k; render(); }}, label, el("span", {className:"n"}, counts[k]))));
}
// A structural signature: it changes when the set/order/state/labels change, but
// not when only a timer ticks. When it matches, the existing rows are reused.
function navSignature(agents, teams, filtering){
  const a = agents.map(x => `${x.agent_id}:${x.state}:${(x.interactions||[]).length}`).sort();
  // Not sorted: the array order is the user's drag order, so reordering rebuilds.
  const t = teams.map(x => `${x.team_id}:${(x.members||[]).map(m=>m.agent_id).join(",")}:${S.collapsed.has(x.team_id)}`);
  const tm = (S.terminals||[]).map(x => `${x.session_id}:${x.status}:${x.readonly}:${x.title||""}:${x.host||""}`).sort();
  return JSON.stringify({ a, t, tm, f: S.filter, q: S.q.trim().toLowerCase(), filtering: !!filtering,
                          inbox: inboxItems().length });
}
function render(){
  if(!S.state) { if(!$("nav").children.length) $("nav").replaceChildren(el("div",{className:"skel"}),el("div",{className:"skel"}),el("div",{className:"skel"})); return; }
  const agents = S.state.agents || [], teams = orderTeams(S.state.teams || []);
  updateChrome(); renderFilters(agents); updateInboxBadge(); renderMissionIfOpen(); renderTimelineIfOpen(); renderUsageIfOpen();
  const grouped = new Set();
  teams.forEach(t => (t.members||[]).forEach(m => grouped.add(m.agent_id)));
  const nav = $("nav");
  const filtering = S.q.trim() || S.filter !== "all";
  const sig = navSignature(agents, teams, filtering);
  if(sig === S.navSig && nav.children.length){
    // Structure unchanged: refresh only the dynamic row bits (state class,
    // selection, "needs answer" badge) instead of rebuilding every row.
    refreshRows(agents);
    syncTerminalSelection();
    renderHead(); renderAsks(); renderActivity(); layout();
    if(S.view==="hist" && S.hist[S.selected]) paintStatus(S.hist[S.selected]);
    return;
  }
  S.navSig = sig;
  const top = nav.scrollTop, kids = [];
  const pending = inboxItems().length;
  if(pending) kids.push(inboxRow(pending));
  teams.forEach(t => {
    const members = (t.members||[]).map(m => agentById(m.agent_id) || m);
    const shown = members.filter(matches);
    if(filtering && !shown.length) return;
    const collapsed = S.collapsed.has(t.team_id) && !filtering;
    const working = members.filter(m => m.state === "working").length;
    const body = el("div", {className:"team-body"},
      t.workspace || t.host ? el("div", {className:"team-ws", style:"padding:6px 8px 4px", title:t.host ? `${t.host}:${t.workspace||""}` : t.workspace},
        ic("folder","sm"), " ", t.host ? `${t.host}:${t.workspace||"~"}` : t.workspace) : null,
      shown.map(a => dndAgentRow(a, false)), !members.length ? el("div", {className:"empty-side"}, "No members yet") : null,
      t.missing?.length ? el("div", {className:"team-missing"}, `× ${t.missing.join(", ")} (missing)`) : null);
    const more = el("button", {className:"btn icon sm ghost", type:"button", "aria-label":`Actions for team ${t.name}`, title:"Team actions",
      onclick:(e)=>{ e.stopPropagation(); openMenu(more, [
        {label:"New agent in team", icon:"plus", run:()=>teamAction("new-agent", t.team_id)},
        {label:"Mission control", icon:"activity", run:()=>openMission(t.team_id)},
        {label:"Add members…", icon:"users", run:()=>teamAction("add-members", t.team_id)},
        {label:"Remove members…", icon:"x", run:()=>teamAction("rm-members", t.team_id)},
        {label:"Set workspace…", icon:"folder", run:()=>teamAction("set-ws", t.team_id)}, "-",
        {label:"Delete team", icon:"trash", danger:true, run:()=>teamAction("rm-team", t.team_id)}]); }}, ic("more","sm"));
    const head = el("div", {className:"team-head", role:"button", tabIndex:0, "aria-expanded":!collapsed,
        draggable:true,
        ondragstart:(e)=>dragStart(e, "team", t.team_id),
        ondragend:dragEnd,
        onclick:()=>{ S.collapsed.has(t.team_id) ? S.collapsed.delete(t.team_id) : S.collapsed.add(t.team_id);
                      store.set("at.collapsed", JSON.stringify([...S.collapsed])); render(); },
        onkeydown:(e)=>{ if(e.key==="Enter"||e.key===" "){ e.preventDefault(); e.currentTarget.click(); } }},
      ic("chev","sm chev"),
      el("div", {className:"team-title"}, el("div", {className:"team-name"}, el("span", {className:"nm"}, t.name),
        el("span", {className:"chip"}, members.length),
        working ? el("span", {className:"chip", style:"color:var(--work)", title:`${working} working`}, dot("working"), working) : null)),
      more);
    const teamEl = el("div", {className:"team" + (collapsed ? " collapsed" : ""), dataset:{team:t.team_id},
        ondragover:(e)=>dragOver(e, teamEl), ondragleave:()=>teamEl.classList.remove("drop-target"),
        ondrop:(e)=>dropTeam(e, t.team_id, teamEl)},
      head, body);
    kids.push(teamEl);
  });
  const terms = (S.terminals||[]).filter(termMatches);
  if(terms.length){ kids.push(el("div", {className:"group-label"}, "Terminals")); terms.forEach(t => kids.push(terminalRow(t))); }
  const order = agentOrderIds(), rank = a => { const i = order.indexOf(a.agent_id); return i < 0 ? 1e9 : i; };
  const ungrouped = agents.filter(a => !grouped.has(a.agent_id) && matches(a)).sort((a,b)=> rank(a) - rank(b));
  if(ungrouped.length){
    const label = el("div", {className:"group-label", ondragover:(e)=>dragOver(e, label),
        ondragleave:()=>label.classList.remove("drop-target"), ondrop:(e)=>dropUngrouped(e, label)}, "Ungrouped");
    kids.push(label);
    ungrouped.forEach(a => kids.push(dndAgentRow(a, true)));
  }
  if(!agents.length) kids.push(el("div", {className:"empty-side"}, el("div", {style:"font-weight:600;color:var(--fg);margin-bottom:4px"}, "No agents yet"), "Create your first agent to get started."));
  else if(!kids.length) kids.push(el("div", {className:"empty-side"}, "No agents match", filtering ? el("div", {}, el("button", {className:"btn sm", style:"margin-top:10px", onclick:()=>{ S.q=""; S.filter="all"; $("q").value=""; syncSearch(); render(); }}, "Clear filters")) : null));
  nav.replaceChildren(...kids); nav.scrollTop = top;
  nav.dataset.sig = sig;
  syncTerminalSelection();
  renderHead(); renderAsks(); renderActivity(); layout();
  if(S.view==="hist" && S.hist[S.selected]) paintStatus(S.hist[S.selected]);
}

function refreshRows(agents){
  const byId = new Map(agents.map(a => [a.agent_id, a]));
  document.querySelectorAll("#nav .agent-row").forEach(row => {
    const a = byId.get(row.dataset.agent); if(!a) return;
    const st = a.state || "unknown";
    row.className = `agent-row ${st} act-${actOf(a).kind}${a.agent_id === S.selected ? " sel" : ""}`
      + (S.doneAt[a.agent_id] && Date.now() - S.doneAt[a.agent_id] < 2400 ? " done" : "")
      + ((a.interactions||[]).length ? " attn" : "");
    row.setAttribute("aria-current", a.agent_id === S.selected ? "true" : "false");
    const actEl = row.querySelector(".sub .act");
    if(actEl){ const t = actText(a); if(actEl.title !== t){ actEl.title = t; actEl.replaceChildren(...subNodes(a)); } }
    const avatar = row.querySelector(".avatar");
    if(avatar){ avatar.className = `avatar ${st}`; const dotEl = avatar.querySelector(".dot"); if(dotEl) dotEl.className = `dot s-${st} a-${actOf(a).kind}`; }
    const badge = row.querySelector(".badge");
    const n = (a.interactions||[]).length;
    if(n && !badge){ /* structure change is caught by the signature on next render */ }
    else if(badge) badge.textContent = n;
  });
}

// Second line of a row: the live activity (thinking / tool / command / waiting…).
function subNodes(a){
  const x = actOf(a), st = a.state || "unknown";
  const out = [ x.detail ? `${x.label} — ${x.detail}` : x.label ];
  if(st === "working" && !x.detail) out.push(" ", timerSpan(a));
  return out;
}
function agentRow(a){
  const st = a.state || "unknown", sel = a.agent_id === S.selected;
  const recent = S.doneAt[a.agent_id] && Date.now() - S.doneAt[a.agent_id] < 2400;
  const n = (a.interactions||[]).length;
  const sub = subNodes(a);
  const more = el("button", {className:"btn icon sm ghost more", type:"button", "aria-label":`Actions for ${a.name||a.agent_id}`,
    onclick:(e)=>{ e.stopPropagation(); openMenu(more, agentMenu(a)); }}, ic("more","sm"));
  const row = el("div", {className:`agent-row ${st} act-${actOf(a).kind}${sel ? " sel" : ""}${recent ? " done" : ""}${n ? " attn" : ""}`,
      dataset:{agent:a.agent_id}, role:"button", tabIndex:0, "aria-current":sel ? "true" : null,
      onclick:()=>{ select(a.agent_id); closeDrawer(); },
      onkeydown:(e)=>{ if(e.key==="Enter"||e.key===" "){ e.preventDefault(); select(a.agent_id); closeDrawer(); } },
      oncontextmenu:(e)=>{ e.preventDefault(); openMenu(more, agentMenu(a)); }},
    el("span", {className:`avatar ${st}`, style:`--h:${hue(a.kind||a.name)}`}, (a.name||a.agent_id||"?").trim().charAt(0) || "?",
      el("span", {className:"st"}, dot(st, actOf(a).kind))),
    el("span", {className:"info"}, el("span", {className:"nm", title:a.name||a.agent_id}, a.name||a.agent_id),
      el("span", {className:"sub"}, el("span", {className:"mono", style:"opacity:.8"}, a.kind), el("span", {style:"opacity:.5"}, "·"), el("span", {className:"act", title:actText(a), style:"overflow:hidden;text-overflow:ellipsis"}, ...sub))),
    n ? el("span", {className:"badge pulse", title:"waiting for your answer"}, n) : null, more);
  return row;
}
function dndAgentRow(a, allowReorder){
  const row = agentRow(a);
  row.draggable = true;
  row.addEventListener("dragstart", (e)=>dragStart(e, "agent", a.agent_id));
  row.addEventListener("dragend", dragEnd);
  row.addEventListener("dragover", (e)=>dragOver(e, row));
  row.addEventListener("dragleave", ()=>row.classList.remove("drop-target"));
  if(allowReorder) row.addEventListener("drop", (e)=>dropAgent(e, a.agent_id, row));
  return row;
}
function agentMenu(a){
  const items = [{label:"Open", icon:"msg", run:()=>select(a.agent_id)},
    {label:"Copy name", icon:"copy", run:()=>copyText(a.name||a.agent_id, "Name copied")}];
  if(a.cwd) items.push({label:"Copy working directory", icon:"folder", run:()=>copyText(a.cwd, "Path copied")});
  if(a.kind==="claude"||a.kind==="opencode") items.push({label:"New session", icon:"refresh", run:()=>newSession(a.agent_id)});
  if(a.state==="working") items.push({label:"Stop current turn", icon:"stop", run:()=>stopTurn(a.agent_id)});
  items.push({label:"Timeline", icon:"activity", run:()=>{ select(a.agent_id); openTimeline(); }});
  items.push("-", {label:"Delete agent", icon:"trash", danger:true, run:()=>deleteAgent(a.agent_id)});
  return items;
}
/* ---------- terminals as first-class rows in the sidebar ---------- */
function termMatches(t){
  const q = S.q.trim().toLowerCase();
  return !q || [t.title, t.session_id, t.host, t.cwd, t.status].some(v => String(v||"").toLowerCase().includes(q));
}
function terminalRow(t){
  const sel = S.selectedTerm === t.session_id;
  const st = t.status || "unknown";
  return el("div", {className:`agent-row terminal ${st}${sel ? " sel" : ""}`,
      dataset:{terminal:t.session_id}, role:"button", tabIndex:0, "aria-current":sel ? "true" : null,
      onclick:()=>{ selectTerminal(t.session_id); closeDrawer(); },
      onkeydown:(e)=>{ if(e.key==="Enter"||e.key===" "){ e.preventDefault(); selectTerminal(t.session_id); closeDrawer(); } }},
    el("span", {className:`avatar ${st}`}, ic("terminal","sm")),
    el("span", {className:"info"},
      el("span", {className:"nm", title:t.title||t.session_id}, t.title || t.session_id),
      el("span", {className:"sub"},
        el("span", {className:"mono", style:"opacity:.8"}, t.host || "local"),
        el("span", {style:"opacity:.5"}, "·"),
        el("span", {className:"act"}, st, t.readonly ? " · read-only" : ""))));
}
function selectTerminal(id){
  S.selected = null; S.selectedTerm = id;
  if(S.termView){ S.termView.dispose(); S.termView = null; }
  render();
}
function mountSelectedTerminal(){
  const t = terminalById(S.selectedTerm);
  if(!t) return;
  const mount = $("termx-mount");
  if(!mount) return;
  S.termView = mountTerminalView(mount, t.session_id, {readonly: !!t.readonly, focus: true});
  S.termView.id = t.session_id;
}
function syncTerminalSelection(){
  if(S.selectedTerm && !terminalById(S.selectedTerm)){
    S.selectedTerm = null;
    if(S.termView){ S.termView.dispose(); S.termView = null; }
  }
  if(S.selectedTerm && !S.termView) mountSelectedTerminal();
}
async function closeSelectedTerminal(){
  if(!S.selectedTerm) return;
  if(!await confirmDlg({title:"Close terminal?", message:"The shell and anything running in it end.", ok:"Close", danger:true})) return;
  try{ await op("terminal_close", {id:S.selectedTerm}); }
  catch(e){ flash(e.message); return; }
  S.selectedTerm = null;
  if(S.termView){ S.termView.dispose(); S.termView = null; }
  render();
}
function renderTerminalHead(){
  const t = terminalById(S.selectedTerm);
  if(!t){ $("head").replaceChildren(); return; }
  const st = t.status || "unknown";
  setKids($("head"),
    el("div", {className:"head-row"},
      el("span", {className:"avatar lg"}, ic("terminal","sm")),
      el("div", {style:"min-width:0;flex:1"},
        el("div", {className:"title"},
          el("span", {className:"nm"}, t.title || t.session_id),
          el("span", {className:"chip mono"}, "terminal"),
          el("span", {className:`pill s-${st}`}, dot(st), st),
          t.readonly ? el("span", {className:"chip"}, ic("lock","sm"), "read-only") : null)),
      el("div", {className:"actions"},
        el("button", {className:"btn", type:"button", title:"Open this terminal in a new tab",
          onclick:()=>{ if(window.Terminals) window.Terminals.openPage(t.session_id); }}, ic("terminal","sm"), el("span", {className:"lbl"}, "Open in tab")),
        el("button", {className:"btn danger", type:"button", id:"terminal-close", title:"Close this terminal",
          onclick:()=>closeSelectedTerminal()}, ic("trash","sm"), el("span", {className:"lbl"}, "Close")))),
    el("div", {className:"meta"},
      el("span", {className:"chip mono"}, ic("terminal","sm"), t.host || "local"),
      t.cwd ? el("span", {className:"chip mono", title:"Working directory"}, ic("folder","sm"), t.cwd) : null,
      t.owner ? el("span", {className:"chip mono", title:"Terminal token"}, ic("lock","sm"), t.owner) : null));
}

function syncSearch(){ $("searchBox").classList.toggle("has", !!S.q); }
$("q").addEventListener("input", ()=>{ S.q = $("q").value; syncSearch(); render(); });
$("q").addEventListener("keydown", e=>{ if(e.key==="Escape"){ $("q").value=""; S.q=""; syncSearch(); render(); $("q").blur(); } });
$("qClear").onclick = ()=>{ $("q").value=""; S.q=""; syncSearch(); render(); $("q").focus(); };

function closeDrawer(){ $("shell").classList.remove("drawer"); }
$("menuBtn").onclick = ()=> $("shell").classList.toggle("drawer");
$("scrim").onclick = closeDrawer;

/* ---------- theme ---------- */
function applyTheme(t){
  if(t) document.documentElement.setAttribute("data-theme", t); else document.documentElement.removeAttribute("data-theme");
  const dark = t ? t === "dark" : !matchMedia("(prefers-color-scheme: light)").matches;
  $("themeBtn").replaceChildren(ic(dark ? "sun" : "moon"));
  $("themeBtn").title = dark ? "Switch to light theme" : "Switch to dark theme";
}
$("themeBtn").onclick = ()=>{
  const cur = document.documentElement.getAttribute("data-theme") || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  const next = cur === "dark" ? "light" : "dark"; store.set("at.theme", next); applyTheme(next); };
applyTheme(store.get("at.theme"));
try { JSON.parse(store.get("at.collapsed") || "[]").forEach(x => S.collapsed.add(x)); } catch(e){}

/* ---------- header / layout ---------- */
function layout(){
  const a = agentById(S.selected);
  const hasAgent = !!a;
  const hasTerm = !!terminalById(S.selectedTerm);
  $("welcome").classList.toggle("show", !hasAgent && !hasTerm);
  $("head").style.display = (hasAgent || hasTerm) ? "" : "none";
  $("tabs").style.display = hasAgent ? "flex" : "none";
  $("term").style.display = hasAgent && S.view==="live" ? "" : "none";
  $("hist").style.display = hasAgent && S.view==="hist" ? "block" : "none";
  $("procs").style.display = hasAgent && S.view==="proc" ? "block" : "none";
  const tx = $("termx");
  if(tx){ tx.classList.toggle("init-hidden", !hasTerm); tx.style.display = hasTerm ? "flex" : "none"; }
  $("termbar").style.display = hasAgent && S.view==="live" ? "flex" : "none";
  $("hist-note").style.display = hasAgent && S.view==="hist" ? "" : "none";
  $("composer-wrap").style.display = hasAgent ? "block" : "none";
  const keys = hasAgent && S.view !== "hist";
  $("keys-wrap").style.display = keys ? "block" : "none"; $("keys").style.display = keys ? "flex" : "none";
  $("progress").className = "progress" + (a && a.state === "working" ? " on" : "");
  updateJump();
}
function renderHead(){
  if(S.selectedTerm){ return renderTerminalHead(); }
  const a = agentById(S.selected);
  if(!a){ $("head").replaceChildren(); return; }
  const teams = (S.state.teams||[]).filter(t=>(t.members||[]).some(m=>m.agent_id===a.agent_id)).map(t=>t.name);
  const st = a.state || "unknown";
  const acts = [];
  if(st === "working") acts.push(el("button", {className:"btn danger", type:"button", title:"Interrupt the running turn (Ctrl+C)",
      onclick:()=>stopTurn(a.agent_id)}, ic("stop","sm"), el("span", {className:"lbl"}, "Stop")));
  if(a.kind==="claude"||a.kind==="opencode") acts.push(el("button", {className:"btn", type:"button", id:"agent-new-session",
      title:"Claude /clear · OpenCode /new", onclick:()=>newSession(a.agent_id)}, ic("refresh","sm"), el("span", {className:"lbl"}, "New session")));
  acts.push(el("button", {className:"btn danger", type:"button", id:"agent-delete", title:"Stop and unregister this agent",
      onclick:()=>deleteAgent(a.agent_id)}, ic("trash","sm"), el("span", {className:"lbl"}, "Delete")));
  const copyChip = (icon, text, what) => el("span", {className:"chip mono copy", role:"button", tabIndex:0, title:`Click to copy · ${text}`,
      onclick:()=>copyText(text, what), onkeydown:(e)=>{ if(e.key==="Enter") copyText(text, what); }}, ic(icon,"sm"), text);
  setKids($("head"),
    el("div", {className:"head-row"},
      el("span", {className:`avatar lg ${st}`, style:`--h:${hue(a.kind||a.name)}`}, (a.name||a.agent_id||"?").trim().charAt(0)),
      el("div", {style:"min-width:0;flex:1"},
        el("div", {className:"title"}, el("span", {className:"nm"}, a.name||a.agent_id), el("span", {className:"chip mono"}, a.kind), pill(a),
          el("span", {className:`act-line act-${actOf(a).kind}`, id:"agent-activity", title:actText(a)}, actText(a)),
          (a.interactions||[]).length ? el("span", {className:"pill s-waiting_input"}, ic("shield","sm"), `${a.interactions.length} awaiting your answer`) : null)),
      el("div", {className:"actions"}, ...acts)),
    el("div", {className:"meta"},
      el("span", {className:"chip mono model", id:"agent-model", title:modelOf(a) ? "Model running this agent" : "Model not observed yet"},
        ic("activity","sm"), modelOf(a) || "model n/d"),
      el("span", {className:"chip mono", title:"Backend"}, ic("terminal","sm"), a.backend||"—"),
      a.host ? el("span", {className:`chip mono host-${a.host_state||"unknown"}`, id:"agent-host",
        title:`Remote host · ${a.host_state||"unknown"}`}, ic("terminal","sm"),
        `${a.host} · ${a.host_state==="unreachable" ? "unreachable, retrying" : a.host_state||"n/d"}`) : null,
      a.cwd ? copyChip("folder", a.cwd, "Path copied") : null,
      a.pid != null ? copyChip("activity", `pid ${a.pid}`, "PID copied") : null,
      a.worktree ? copyChip("folder", a.worktree.branch, "Branch copied") : null,
      el("span", {className:"chip", title:"Teams"}, ic("users","sm"), teams.join(", ") || "no team")),
    a.host_state === "unreachable" || a.host_state === "reconnecting"
      ? el("div", {className:"evidence", id:"agent-host-banner"},
          `⚠ host ${a.host} ${a.host_state === "reconnecting" ? "is reconnecting" : "is unreachable"} — the agent keeps running there; state may be stale`) : null,
    a.worktree_warning ? el("div", {className:"evidence"}, "⚠ " + a.worktree_warning) : null,
    a.evidence ? el("div", {className:"evidence", title:a.evidence}, a.evidence) : null);
  // Gate input by agent state: while WORKING/STARTING the agent rejects input
  // (no queue by design), so disable and explain instead of silently failing.
  $("tab-hist").style.display = a.history ? "" : "none";
  if(!a.history && S.view==="hist") setView("live");
  const hostDown = a.host_state === "unreachable" || a.host_state === "reconnecting";
  const usable = (st === "ready" || st === "waiting_input") && !hostDown;
  const dead = (st === "exited" || st === "error");
  const box = $("input"), btn = $("send");
  box.disabled = !usable; btn.disabled = !usable;
  $("composer").classList.toggle("locked", !usable);
  $("composer-ic").replaceChildren(...(st==="working" ? [dot("working")] : st==="starting" ? [dot("starting")] : dead ? [ic("alert")] : []));
  if (hostDown) box.placeholder = `Host ${a.host} is ${a.host_state === "reconnecting" ? "reconnecting" : "unreachable"} — input is disabled until it is back`;
  else if (dead) box.placeholder = `${a.name||a.agent_id} is ${st} — start it again to interact`;
  else if (st === "working") box.placeholder = "Agent working… input is disabled until it finishes";
  else if (st === "starting") box.placeholder = "Agent starting…";
  else box.placeholder = "Type and press Enter to send to the agent…";
}

/* ---------- main-pane terminal controls ---------- */
if($("termx-claim")) $("termx-claim").onclick = ()=>{ if(S.termView) S.termView.claim(); };
if($("termx-open")) $("termx-open").onclick = ()=>{ if(S.selectedTerm && window.Terminals) window.Terminals.openPage(S.selectedTerm); };
if($("termx-close")) $("termx-close").onclick = ()=>closeSelectedTerminal();
document.addEventListener("keydown", (e)=>{
  if((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f" && S.selectedTerm){
    e.preventDefault();
    const s = $("termx-search"); if(s){ s.focus(); s.select(); }
  }
});
