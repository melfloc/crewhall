"use strict";
function agentById(id){ return (S.state?.agents||[]).find(a=>a.agent_id===id); }
function teamById(id){ return (S.state?.teams||[]).find(t=>t.team_id===id); }
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
  const t = teams.map(x => `${x.team_id}:${(x.members||[]).map(m=>m.agent_id).join(",")}:${S.collapsed.has(x.team_id)}`).sort();
  return JSON.stringify({ a, t, f: S.filter, q: S.q.trim().toLowerCase(), filtering: !!filtering,
                          inbox: inboxItems().length });
}
function render(){
  if(!S.state) { if(!$("nav").children.length) $("nav").replaceChildren(el("div",{className:"skel"}),el("div",{className:"skel"}),el("div",{className:"skel"})); return; }
  const agents = S.state.agents || [], teams = S.state.teams || [];
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
      t.workspace ? el("div", {className:"team-ws", style:"padding:6px 8px 4px", title:t.workspace}, ic("folder","sm"), " ", t.workspace) : null,
      shown.map(agentRow), !members.length ? el("div", {className:"empty-side"}, "No members yet") : null,
      t.missing?.length ? el("div", {className:"team-missing"}, `× ${t.missing.join(", ")} (missing)`) : null);
    const more = el("button", {className:"btn icon sm ghost", type:"button", "aria-label":`Actions for team ${t.name}`, title:"Team actions",
      onclick:(e)=>{ e.stopPropagation(); openMenu(more, [
        {label:"New agent in team", icon:"plus", run:()=>teamAction("new-agent", t.team_id)},
        {label:"Mission control", icon:"activity", run:()=>openMission(t.team_id)},
        {label:"Add members…", icon:"users", run:()=>teamAction("add-members", t.team_id)},
        {label:"Remove members…", icon:"x", run:()=>teamAction("rm-members", t.team_id)},
        {label:"Set workspace…", icon:"folder", run:()=>teamAction("set-ws", t.team_id)}, "-",
        {label:"Delete team", icon:"trash", danger:true, run:()=>teamAction("rm-team", t.team_id)}]); }}, ic("more","sm"));
    kids.push(el("div", {className:"team" + (collapsed ? " collapsed" : ""), dataset:{team:t.team_id}},
      el("div", {className:"team-head", role:"button", tabIndex:0, "aria-expanded":!collapsed,
          onclick:()=>{ S.collapsed.has(t.team_id) ? S.collapsed.delete(t.team_id) : S.collapsed.add(t.team_id);
                        store.set("at.collapsed", JSON.stringify([...S.collapsed])); render(); },
          onkeydown:(e)=>{ if(e.key==="Enter"||e.key===" "){ e.preventDefault(); e.currentTarget.click(); } }},
        ic("chev","sm chev"),
        el("div", {className:"team-title"}, el("div", {className:"team-name"}, el("span", {className:"nm"}, t.name),
          el("span", {className:"chip"}, members.length),
          working ? el("span", {className:"chip", style:"color:var(--work)", title:`${working} working`}, dot("working"), working) : null)),
        more), body));
  });
  const ungrouped = agents.filter(a => !grouped.has(a.agent_id) && matches(a));
  if(ungrouped.length){ kids.push(el("div", {className:"group-label"}, "Ungrouped")); ungrouped.forEach(a => kids.push(agentRow(a))); }
  if(!agents.length) kids.push(el("div", {className:"empty-side"}, el("div", {style:"font-weight:600;color:var(--fg);margin-bottom:4px"}, "No agents yet"), "Create your first agent to get started."));
  else if(!kids.length) kids.push(el("div", {className:"empty-side"}, "No agents match", filtering ? el("div", {}, el("button", {className:"btn sm", style:"margin-top:10px", onclick:()=>{ S.q=""; S.filter="all"; $("q").value=""; syncSearch(); render(); }}, "Clear filters")) : null));
  nav.replaceChildren(...kids); nav.scrollTop = top;
  nav.dataset.sig = sig;
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
  const has = !!S.selected, a = agentById(S.selected);
  $("welcome").classList.toggle("show", !has);
  $("head").style.display = a ? "" : "none";
  $("tabs").style.display = has ? "flex" : "none";
  $("term").style.display = has && S.view==="live" ? "" : "none";
  $("hist").style.display = has && S.view==="hist" ? "block" : "none";
  $("procs").style.display = has && S.view==="proc" ? "block" : "none";
  $("termbar").style.display = has && S.view==="live" ? "flex" : "none";
  $("hist-note").style.display = S.view==="hist" ? "" : "none";
  $("composer-wrap").style.display = has ? "block" : "none";
  const keys = has && S.view !== "hist";
  $("keys-wrap").style.display = keys ? "block" : "none"; $("keys").style.display = keys ? "flex" : "none";
  $("progress").className = "progress" + (a && a.state === "working" ? " on" : "");
  updateJump();
}
function renderHead(){
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
