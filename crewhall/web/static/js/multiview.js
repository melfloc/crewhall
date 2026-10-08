"use strict";
/* Multi-view: a full-screen overlay showing several agents' viewports at once
   in a grid, plus a single-agent "full view". It is a self-contained layer that
   reuses the existing render helpers (histNode / ansiLine / askCard) and talks
   to the daemon through the same op() transport. Nothing here touches the main
   viewport's DOM, so both can coexist. */

const MV = {
  open: false,
  mode: "grid",            // "grid" | "full"
  returnToGrid: false,     // full view opened from the grid -> Esc goes back
  focus: null,             // agent id in full view
  layout: store.get("at.mv.layout") || "auto",
  ids: null,               // ordered agent ids shown in the grid
  auto: store.get("at.mv.auto") !== "0",  // mirror every agent until the user curates
  cells: {},               // agent id -> per-cell state
};

const MV_LAYOUTS = [
  ["auto", "Auto (tiling)", "i-grid"],
  ["1", "Single", "i-expand"],
  ["2c", "2 columns", "i-columns"],
  ["2r", "2 rows", "i-rows"],
  ["3c", "3 columns", "i-columns"],
  ["2x2", "2 × 2 grid", "i-grid"],
  ["3x2", "3 × 2 grid", "i-grid"],
];
const MV_CAP = { "1": 1, "2c": 2, "2r": 2, "3c": 3, "2x2": 4, "3x2": 6 };

/* ---------- state ---------- */
function mvCellSt(id){
  return MV.cells[id] || (MV.cells[id] = {view:"hist", follow:true, conv:undefined,
    sig:null, total:0, loading:false, pending:null, liveSig:null, asksKey:null, refs:null});
}
function mvLoadIds(){
  try { const v = JSON.parse(store.get("at.mv.agents") || "[]"); return Array.isArray(v) ? v : []; }
  catch(e){ return []; }
}
function mvSaveIds(){ store.set("at.mv.agents", JSON.stringify(MV.ids || [])); }
function mvSaveAuto(){ store.set("at.mv.auto", MV.auto ? "1" : "0"); }
function mvSaveLayout(l){ MV.layout = l; store.set("at.mv.layout", l); }
function mvCap(){ return MV_CAP[MV.layout] || 99; }
function mvLayoutLabel(){ const row = MV_LAYOUTS.find(x => x[0] === MV.layout); return row ? row[1] : "Auto (tiling)"; }
/* The user curated the list (added/removed/reordered): stop mirroring every agent. */
function mvCurated(){ MV.auto = false; mvSaveAuto(); }

function mvResolveIds(){
  const agents = (S.state && S.state.agents) || [];
  const valid = new Set(agents.map(a => a.agent_id));
  if(MV.ids === null) MV.ids = mvLoadIds();
  let ids = (MV.ids || []).filter(id => valid.has(id));
  if(MV.auto){
    // Mirror the live agent set: drop gone ones (done by the filter) and append new ones.
    agents.forEach(a => { if(!ids.includes(a.agent_id)) ids.push(a.agent_id); });
  }
  if(!ids.length && agents.length){
    const ordered = agents.map(a => a.agent_id);
    ids = (S.selected && valid.has(S.selected))
      ? [S.selected].concat(ordered.filter(x => x !== S.selected))
      : ordered;
  }
  MV.ids = ids;
  return ids;
}
function mvVisibleIds(){
  const cap = mvCap();
  return mvResolveIds().slice(0, cap);
}

/* ---------- open / close / full view ---------- */
function mvOpenGrid(){
  MV.open = true; MV.mode = "grid"; MV.focus = null;
  mvResolveIds();
  $("multi").classList.remove("init-hidden");
  mvRender();
  mvStartTimer();
}
function mvClose(){
  MV.open = false; MV.mode = "grid"; MV.focus = null;
  $("multi").classList.add("init-hidden");
  mvStopTimer();
}
function mvOpenFull(id, opts){
  if(!id) return;
  opts = opts || {};
  const wasGrid = MV.open && MV.mode === "grid";
  MV.open = true; MV.mode = "full"; MV.focus = id; MV.returnToGrid = !!wasGrid;
  mvResolveIds();
  $("multi").classList.remove("init-hidden");
  mvRender();
  mvStartTimer();
}
function mvExitFull(){
  if(MV.returnToGrid){ MV.mode = "grid"; MV.focus = null; MV.returnToGrid = false; mvRender(); }
  else mvClose();
}

/* ---------- rendering ---------- */
function mvRender(){
  const root = $("multi");
  if(!root || !MV.open) return;
  if(MV.mode === "full") return mvRenderFull(root);
  return mvRenderGrid(root);
}

function mvRenderGrid(root){
  const ids = mvVisibleIds();
  MV._sig = ids.join(",") + "|" + MV.layout;
  root.replaceChildren(mvToolbar(), mvGrid(ids));
}

function mvGrid(ids){
  if(MV.layout === "auto") return mvTileGrid(ids);
  const grid = el("div", {className:"mv-grid", dataset:{layout:MV.layout}});
  if(!ids.length){
    grid.append(el("div", {className:"mv-empty"}, "No agents shown. Use “Add agent” to bring one in."));
    return grid;
  }
  ids.forEach(id => grid.append(mvCell(id)));
  return grid;
}

/* Auto layout: binary space partition ("dwindle", like Hyprland/Omarchy). The
   first split halves the screen (50/50), the next split halves the largest
   remaining pane along its longer side, and so on. Each pane is placed with CSS
   grid tracks derived from the partition, so the tiling is exact at any size. */
function mvTileRects(n){
  const rects = [{x:0, y:0, w:1, h:1}];
  while(rects.length < n){
    let idx = 0, best = -1;
    rects.forEach((r, i) => { const area = r.w * r.h; if(area >= best){ best = area; idx = i; } });
    const r = rects.splice(idx, 1)[0];
    if(r.w >= r.h){  // wider than tall -> split vertically
      rects.splice(idx, 0, {x:r.x, y:r.y, w:r.w/2, h:r.h},
                          {x:r.x + r.w/2, y:r.y, w:r.w/2, h:r.h});
    } else {          // taller than wide -> split horizontally
      rects.splice(idx, 0, {x:r.x, y:r.y, w:r.w, h:r.h/2},
                          {x:r.x, y:r.y + r.h/2, w:r.w, h:r.h/2});
    }
  }
  return rects;
}
function mvTileGrid(ids){
  const grid = el("div", {className:"mv-grid tiled", dataset:{layout:"auto"}});
  if(!ids.length){
    grid.append(el("div", {className:"mv-empty"}, "No agents shown. Use “Add agent” to bring one in."));
    return grid;
  }
  const rects = mvTileRects(ids.length);
  const xs = [...new Set(rects.flatMap(r => [r.x, r.x + r.w]))].sort((a, b) => a - b);
  const ys = [...new Set(rects.flatMap(r => [r.y, r.y + r.h]))].sort((a, b) => a - b);
  applyStyle(grid, `grid-template-columns:repeat(${xs.length - 1},minmax(0,1fr));`
                 + `grid-template-rows:repeat(${ys.length - 1},minmax(0,1fr))`);
  ids.forEach((id, i) => {
    const r = rects[i];
    const cell = mvCell(id);
    applyStyle(cell, `grid-column:${xs.indexOf(r.x) + 1} / ${xs.indexOf(r.x + r.w) + 1};`
                   + `grid-row:${ys.indexOf(r.y) + 1} / ${ys.indexOf(r.y + r.h) + 1}`);
    grid.append(cell);
  });
  return grid;
}

function mvToolbar(){
  const layoutBtn = el("button", {className:"btn sm", type:"button", title:"Grid layout",
    onclick:(e)=>{ e.stopPropagation(); openMenu(layoutBtn, mvLayoutMenu()); }},
    ic("grid","sm"), el("span", {className:"lbl"}, mvLayoutLabel()));
  const addBtn = el("button", {className:"btn sm", type:"button", title:"Add an agent to the grid",
    onclick:(e)=>{ e.stopPropagation(); openMenu(addBtn, mvAddMenu()); }},
    ic("plus","sm"), el("span", {className:"lbl"}, "Add agent"));
  return el("div", {className:"mv-bar"},
    el("span", {className:"mv-brand"}, ic("grid","sm"), "Multi-view"),
    layoutBtn, addBtn,
    el("span", {className:"spacer"}),
    el("span", {className:"mv-count"}, `${mvVisibleIds().length} shown`),
    el("button", {className:"btn icon sm ghost", type:"button", title:"Close multi-view (Esc)",
      onclick:()=>mvClose()}, ic("x","sm")));
}

function mvLayoutMenu(){
  return MV_LAYOUTS.map(([id, label, icon]) =>
    ({label: label + (id === MV.layout ? "  ✓" : ""), icon, run:()=>{ mvSaveLayout(id); mvRender(); }}));
}
function mvAddMenu(){
  const shown = new Set(mvVisibleIds());
  const rest = (S.state?.agents || []).filter(a => !shown.has(a.agent_id));
  if(!rest.length) return [{label:"Every agent is already shown", icon:"check", run:()=>{}}];
  return rest.map(a => ({label: a.name || a.agent_id, icon:"users", run:()=>mvAdd(a.agent_id)}));
}
function mvAdd(id){
  mvResolveIds();
  if(!MV.ids.includes(id)) MV.ids.push(id);
  mvCurated(); mvSaveIds(); mvRender();
}
function mvRemove(id){
  MV.ids = mvResolveIds().filter(x => x !== id);
  mvCurated(); mvSaveIds(); mvRender();
}
function mvSetCell(oldId, newId){
  const ids = mvResolveIds().map(x => x === oldId ? newId : x);
  MV.ids = ids.filter((x, i) => ids.indexOf(x) === i);   // no duplicates
  mvCurated(); mvSaveIds(); mvRender();
}

function mvCell(id, full){
  const a = agentById(id) || {agent_id:id, name:id, state:"unknown"};
  const st = mvCellSt(id);
  const head = mvCellHead(id);
  const body = el("div", {className: mvBodyClass(st.view)});
  const asks = el("div", {className:"mv-asks"});
  const composer = mvComposer(id);
  const cell = el("div", {className:"mv-cell" + (full ? " full" : ""), dataset:{agent:id},
      draggable: !full,
      ondragstart: (e)=>mvDragStart(e, id),
      ondragover: (e)=>mvDragOver(e, cell),
      ondragleave: ()=>cell.classList.remove("mv-drop"),
      ondrop: (e)=>mvDrop(e, id, cell),
      ondragend: mvDragEnd},
    head, body, asks, composer);
  st.refs = {head, body, asks, composer};
  body.addEventListener("scroll", ()=>{
    st.follow = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
  });
  mvUpdateHead(id);
  mvUpdateAsks(id);
  if(st.view === "live") mvRenderLive(id, true); else mvRefreshHistory(id, true);
  return cell;
}

function mvCellHead(id){
  const a = agentById(id) || {agent_id:id, name:id, state:"unknown"};
  const state = a.state || "unknown";
  const pillBox = el("span", {className:"mv-pill"});
  const actEl = el("span", {className:"mv-act", title:actText(a)}, actText(a));
  const viewBtn = el("button", {className:"btn icon sm ghost", type:"button", title:"Conversation / Live",
    "aria-label":"Toggle conversation or live view", onclick:()=>mvToggleView(id)}, ic("msg","sm"));
  const expBtn = el("button", {className:"btn icon sm ghost", type:"button", title:"Full view",
    "aria-label":"Full view", onclick:()=>mvOpenFull(id, {})}, ic("expand","sm"));
  const moreBtn = el("button", {className:"btn icon sm ghost", type:"button", title:"Agent options",
    "aria-label":"Agent options", onclick:(e)=>{ e.stopPropagation(); openMenu(moreBtn, mvCellMenu(id)); }}, ic("more","sm"));
  const removeBtn = el("button", {className:"btn icon sm ghost mv-remove", type:"button",
    title:"Remove this agent from the multi-view", "aria-label":"Remove from multi-view",
    onclick:(e)=>{ e.stopPropagation(); mvRemove(id); }}, ic("x","sm"));
  return el("div", {className:"mv-cell-head"},
    el("span", {className:`avatar ${state}`, style:`--h:${hue(a.kind||a.name||id)}`},
      (a.name || id).trim().charAt(0) || "?", el("span", {className:"st"}, dot(state, actOf(a).kind))),
    el("div", {className:"mv-cell-id"},
      el("button", {className:"mv-name", type:"button", title:"Open this agent in the main view",
        onclick:()=>select(id)}, a.name || id),
      el("span", {className:"mv-sub"}, actEl)),
    pillBox, viewBtn, expBtn, moreBtn, removeBtn);
}

function mvCellMenu(id){
  const items = [
    {label:"Conversation", icon:"msg", run:()=>mvSetView(id, "hist")},
    {label:"Live output", icon:"activity", run:()=>mvSetView(id, "live")},
    {label:"Full view", icon:"expand", run:()=>mvOpenFull(id, {})},
    {label:"Open in main view", icon:"terminal", run:()=>select(id)},
    "-",
  ];
  const others = (S.state?.agents || []).filter(a => a.agent_id !== id);
  others.slice(0, 12).forEach(a => items.push(
    {label:"Show here: " + (a.name || a.agent_id), icon:"users", run:()=>mvSetCell(id, a.agent_id)}));
  items.push("-", {label:"Remove from view", icon:"x", danger:true, run:()=>mvRemove(id)});
  return items;
}

function mvComposer(id){
  const a = agentById(id) || {agent_id:id, state:"unknown"};
  const state = a.state || "unknown";
  const usable = mvUsable(a);
  const ta = el("textarea", {className:"mv-input", rows:1, "aria-label":"Message for the agent",
    placeholder: usable ? "Message… (Enter to send)" : `${a.name || id} is ${state} — input disabled`,
    disabled: !usable});
  const sendBtn = el("button", {className:"btn primary sm", type:"button", disabled: !usable,
    title:"Send", onclick:()=>mvSend(id)}, ic("send","sm"));
  ta.addEventListener("keydown", (e)=>{
    if(e.isComposing) return;
    if(e.key === "Enter" && !e.shiftKey){ e.preventDefault(); mvSend(id); }
  });
  ta.addEventListener("input", ()=>{ ta.style.height = "auto"; ta.style.height = Math.min(140, ta.scrollHeight) + "px"; });
  return el("div", {className:"mv-composer"}, ta, sendBtn);
}

function mvUsable(a){
  const state = a.state || "unknown";
  return (state === "ready" || state === "waiting_input")
    && a.host_state !== "unreachable" && a.host_state !== "reconnecting";
}

/* Full view: one cell filling the window. */
function mvRenderFull(root){
  const id = MV.focus;
  const a = agentById(id) || {agent_id:id, name:id, state:"unknown"};
  const pillBox = el("span", {className:"mv-pill"});
  const bar = el("div", {className:"mv-bar"},
    el("button", {className:"btn sm", type:"button", onclick:()=>mvExitFull()},
      ic("collapse","sm"), el("span", {className:"lbl"}, MV.returnToGrid ? "Back to grid" : "Close")),
    el("span", {className:"mv-brand"}, ic("expand","sm"), "Full view"),
    el("span", {className:"mv-name-lg"}, a.name || id),
    pillBox,
    el("span", {className:"spacer"}),
    el("button", {className:"btn icon sm ghost", type:"button", title:"Close (Esc)",
      onclick:()=>mvExitFull()}, ic("x","sm")));
  root.replaceChildren(bar, mvCell(id, true));
  pillBox.replaceChildren(pill(a));
}

/* ---------- per-cell updates ---------- */
function mvBodyClass(view){ return view === "live" ? "term mv-body" : "hist mv-body"; }

function mvUpdateHead(id){
  const st = MV.cells[id];
  if(!st || !st.refs) return;
  const a = agentById(id) || {agent_id:id, state:"unknown"};
  const state = a.state || "unknown";
  const av = st.refs.head.querySelector(".avatar");
  if(av){ av.className = `avatar ${state}`; const d = av.querySelector(".dot");
    if(d) d.className = `dot s-${state} a-${actOf(a).kind}`; }
  const pb = st.refs.head.querySelector(".mv-pill"); if(pb) pb.replaceChildren(pill(a));
  const act = st.refs.head.querySelector(".mv-act");
  if(act){ const t = actText(a); if(act.title !== t){ act.title = t; act.textContent = t; } }
  const ta = st.refs.composer.querySelector(".mv-input");
  const btn = st.refs.composer.querySelector("button");
  const usable = mvUsable(a);
  if(ta){ ta.disabled = !usable;
    if(!ta.value) ta.placeholder = usable ? "Message… (Enter to send)" : `${a.name || id} is ${state} — input disabled`; }
  if(btn) btn.disabled = !usable;
}

function mvUpdateAsks(id){
  const st = MV.cells[id];
  if(!st || !st.refs) return;
  const a = agentById(id) || {};
  const items = a.interactions || [];
  const key = items.map(i => i.id).join("|");
  if(st.asksKey === key) return;
  st.asksKey = key;
  st.refs.asks.replaceChildren(...items.map(it => askCard(it)));
  st.refs.asks.classList.toggle("has", items.length > 0);
}

function mvPaintBody(id, nodes){
  const st = MV.cells[id];
  if(!st || !st.refs) return;
  const body = st.refs.body;
  body.className = mvBodyClass(st.view);
  body.replaceChildren(...nodes);
  if(st.follow) body.scrollTop = body.scrollHeight;
}

async function mvRefreshHistory(id, force){
  const st = mvCellSt(id);
  if(!MV.open || st.view !== "hist" || st.loading || !st.refs) return;
  st.loading = true;
  try {
    const r = await op("agent_history", {target:id, limit:80});
    if(!MV.open || st.view !== "hist" || !st.refs) return;
    if(!r.available){
      mvPaintBody(id, [el("div", {className:"mv-note"}, "No conversation history — use the Live view.")]);
      return;
    }
    const last = r.messages && r.messages.length ? msgSig(r.messages[r.messages.length - 1]) : "";
    const sig = `${r.conversation_id}|${r.total}|${last}`;
    if(sig === st.sig && !force) return;
    st.sig = sig; st.total = r.total;
    if(st.pending && (r.messages || []).some(m => m.role === "user" && String(m.text || "").includes(st.pending)))
      st.pending = null;
    const nodes = (r.messages || []).map(m => histNode(m));
    if(st.pending){
      const p = histNode({role:"user", text:st.pending, blocks:[{type:"text", text:st.pending}], at:new Date().toISOString()});
      p.classList.add("pending"); nodes.push(p);
    }
    mvPaintBody(id, nodes);
  } catch(e){ /* transient */ }
  finally { st.loading = false; }
}

function mvRenderLive(id, force){
  const st = MV.cells[id];
  if(!st || st.view !== "live" || !st.refs) return;
  const out = S.output[id] || "";
  if(st.liveSig === out && !force) return;
  st.liveSig = out;
  const frag = document.createDocumentFragment();
  if(!out){ frag.append(document.createTextNode("(no output yet)")); }
  else {
    const lines = out.split("\n");
    lines.forEach((ln, i) => { ansiLine(ln.replace(/\r$/, ""), frag); if(i < lines.length - 1) frag.append(document.createTextNode("\n")); });
  }
  const body = st.refs.body;
  body.className = mvBodyClass("live");
  body.replaceChildren(frag);
  if(st.follow) body.scrollTop = body.scrollHeight;
}

function mvSetView(id, view){
  const st = mvCellSt(id);
  if(st.view === view) return;
  st.view = view; st.sig = null; st.liveSig = null;
  if(view === "live"){ mvRenderLive(id, true); if(!S.output[id]) refreshOutput(id).then(()=>mvRenderLive(id, true)); }
  else mvRefreshHistory(id, true);
}
function mvToggleView(id){ mvSetView(id, mvCellSt(id).view === "hist" ? "live" : "hist"); }

async function mvSend(id){
  const st = MV.cells[id];
  if(!st || !st.refs) return;
  const ta = st.refs.composer.querySelector(".mv-input");
  const text = ta.value;
  if(!text.trim() || ta.disabled) return;
  ta.value = ""; ta.style.height = "auto";
  try {
    await op("agent_write", {target:id, text});
    await op("agent_key", {target:id, key:"ENTER"});
    st.pending = text;
    if(st.view === "hist") mvRefreshHistory(id, true);
    else { mvRenderLive(id, true); setTimeout(()=>{ if(MV.open) mvRenderLive(id, true); }, 600); }
  } catch(e){ flash(e.message); if(!ta.value){ ta.value = text; } }
}

/* ---------- refresh loop + transport hooks ---------- */
let mvTimer = null;
function mvStartTimer(){
  if(mvTimer) return;
  mvTimer = setInterval(()=>{ if(MV.open && !document.hidden) mvRefreshAll(); }, 2000);
}
function mvStopTimer(){ if(mvTimer){ clearInterval(mvTimer); mvTimer = null; } }

function mvRefreshAll(){
  if(!MV.open) return;
  const ids = MV.mode === "full" ? (MV.focus ? [MV.focus] : []) : mvVisibleIds();
  ids.forEach(id => {
    const st = MV.cells[id];
    if(!st || !st.refs) return;
    mvUpdateHead(id);
    mvUpdateAsks(id);
    if(st.view === "live"){
      mvRenderLive(id);
      if(!S.output[id]) refreshOutput(id).then(()=>mvRenderLive(id, true));
    } else mvRefreshHistory(id);
  });
}

/* Called from transport on every state push. */
function mvSync(){
  if(!MV.open) return;
  const valid = new Set(((S.state && S.state.agents) || []).map(a => a.agent_id));
  if(MV.mode === "full"){
    if(MV.focus && !valid.has(MV.focus)){ mvClose(); return; }
    mvUpdateHead(MV.focus); mvUpdateAsks(MV.focus); mvRefreshAll(); return;
  }
  const vis = mvVisibleIds();
  const sig = vis.join(",") + "|" + MV.layout;
  if(sig !== MV._sig){ mvRender(); return; }   // an agent appeared/disappeared or order changed
  mvRefreshAll();
}
/* Called from transport when an agent's live output changes. */
function mvOnOutput(id){
  const st = MV.cells[id];
  if(st && st.view === "live" && st.refs) mvRenderLive(id);
}

/* ---------- drag & drop: swap agents between cells ---------- */
let MV_DRAG = null;
function mvDragStart(e, id){
  MV_DRAG = id;
  try { e.dataTransfer.setData("text/plain", id); e.dataTransfer.effectAllowed = "move"; } catch(_){}
  e.currentTarget.classList.add("dragging");
}
function mvDragOver(e, cell){ if(MV_DRAG){ e.preventDefault(); cell.classList.add("mv-drop"); } }
function mvDragEnd(){
  document.querySelectorAll(".mv-drop, .mv-cell.dragging").forEach(n => n.classList.remove("mv-drop", "dragging"));
  MV_DRAG = null;
}
function mvDrop(e, id, cell){
  e.preventDefault(); cell.classList.remove("mv-drop");
  if(!MV_DRAG || MV_DRAG === id){ MV_DRAG = null; return; }
  const arr = mvResolveIds().slice();
  const i = arr.indexOf(MV_DRAG), j = arr.indexOf(id);
  if(i >= 0 && j >= 0){ arr[i] = id; arr[j] = MV_DRAG; MV.ids = arr; mvCurated(); mvSaveIds(); mvRender(); }
  MV_DRAG = null;
}

/* ---------- wiring ---------- */
$("multiBtn").onclick = ()=>{ if(MV.open && MV.mode === "grid") mvClose(); else mvOpenGrid(); };
$("fullBtn").onclick = ()=>{
  if(MV.open && MV.mode === "full"){ mvExitFull(); return; }
  if(!S.selected){ toast("Select an agent first", "info", 2000); return; }
  mvOpenFull(S.selected, {});
};
document.addEventListener("keydown", (e)=>{
  if(e.altKey && !e.ctrlKey && !e.metaKey && (e.key === "m" || e.key === "M")){
    e.preventDefault(); if(MV.open && MV.mode === "grid") mvClose(); else mvOpenGrid(); return;
  }
  if(e.key === "Escape" && MV.open){
    if(document.querySelector(".menu")) return;   // let the menu close first
    e.preventDefault(); if(MV.mode === "full") mvExitFull(); else mvClose();
  }
});
