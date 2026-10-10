"use strict";
/* ---------- command palette (Ctrl+K) and shortcuts palette (Ctrl+Shift+K) ---------- */
const P = { open:false, items:[], all:[], i:0, kind:"commands" };
/* The agent the palette acts on: the chat's agent in Chat mode, else the selection. */
function paletteTarget(){
  if(typeof currentInterface === "function" && currentInterface() === "chat")
    return (typeof CHAT !== "undefined" && CHAT.info && CHAT.info.agent_id) || null;
  return S.selected || null;
}
function paletteCommands(){
  const cmds = [
    {label:"Interface: Chat", icon:"msg", run:()=>setInterface("chat")},
    {label:"Interface: Cowork", icon:"users", run:()=>setInterface("cowork")},
    {label:"Send a key… (Esc, Ctrl+C, arrows…)", icon:"terminal", run:()=>openPalette("keys")},
    {label:"New agent", icon:"plus", run:()=>openNewAgent()},
    {label:"New team", icon:"users", run:()=>newTeam()},
  ];
  const attn = inboxItems().length;
  if(attn) cmds.push({label:`Needs your answer (${attn})`, icon:"shield", run:()=>openInbox()});
  cmds.push({label:"Mission control", icon:"activity", run:()=>openMission(null)});
  (S.state?.teams||[]).forEach(t => cmds.push({label:`Mission control: ${t.name}`, icon:"activity", run:()=>openMission(t.team_id)}));
  cmds.push({label:"Timeline", icon:"list", run:()=>openTimeline()});
  cmds.push({label:"Cost & usage", icon:"shield", run:()=>openUsage()});
  cmds.push({label:"Search all conversations…", icon:"search", run:()=>openGlobalSearch()});
  cmds.push({label:"Clean up (temp, backups, releases)", icon:"trash", run:()=>openClean()});
  cmds.push({label:"Archived agents", icon:"terminal", run:()=>openArchived()});
  cmds.push({label:"Setup wizard", icon:"plus", run:()=>openOnboarding()});
  if(S.selected){
    cmds.push({label:"Go to Conversation", icon:"msg", run:()=>setView("hist")});
    cmds.push({label:"Go to Live", icon:"activity", run:()=>setView("live")});
    cmds.push({label:"Go to Processes", icon:"terminal", run:()=>setView("proc")});
    cmds.push({label:"Change model…", icon:"activity", run:()=>{ const a=agentById(S.selected); if(a) openModelPicker(a); }});
    cmds.push({label:"Export conversation (Markdown)", icon:"file", run:()=>exportConversation("md")});
  }
  (S.state?.agents || []).forEach(a => {
    cmds.push({label:`Switch to ${a.name||a.agent_id}`, icon:"terminal", run:()=>{ select(a.agent_id); }});
    cmds.push({label:`Send to ${a.name||a.agent_id}…`, icon:"send", run:()=>{ select(a.agent_id); $("input").focus(); }});
    if(a.state === "working") cmds.push({label:`Stop ${a.name||a.agent_id}`, icon:"stop", danger:true, run:()=>stopTurn(a.agent_id)});
  });
  return cmds;
}
/* The shortcuts palette: every named key the agent's TUI understands. */
function paletteKeys(){
  const id = paletteTarget();
  if(!id || typeof KEY_DEFS === "undefined") return [];
  return keyCommands(id);
}
function paletteFilter(){
  const q = $("palette-q").value.trim().toLowerCase();
  P.items = q ? P.all.filter(c => c.label.toLowerCase().includes(q)) : P.all.slice();
  P.i = 0;
  renderPalette();
}
function renderPalette(){
  const list = $("palette-list");
  if(!P.items.length){ list.replaceChildren(el("div", {className:"palette-empty"},
    P.kind === "keys" ? "Select an agent to send keys to." : "No matching commands")); return; }
  list.replaceChildren(...P.items.map((c, i) => el("button", {className:"palette-item" + (i===P.i ? " on" : "") + (c.danger ? " danger" : ""),
    type:"button", role:"option", "aria-selected":i===P.i, onclick:()=>paletteRun(c)}, ic(c.icon, "sm"), c.label)));
}
function paletteMove(d){
  if(!P.items.length) return;
  P.i = (P.i + d + P.items.length) % P.items.length;
  renderPalette();
  const on = $("palette-list").querySelector(".palette-item.on"); if(on) on.scrollIntoView({block:"nearest"});
}
function paletteRun(c){ closePalette(); if(c && c.run) c.run(); }
function openPalette(kind){
  if(P.open){ if(P.kind === (kind || "commands")) return; closePalette(); }
  P.open = true; P.kind = kind === "keys" ? "keys" : "commands";
  P.all = P.kind === "keys" ? paletteKeys() : paletteCommands();
  $("palette-q").value = "";
  $("palette-q").placeholder = P.kind === "keys"
    ? "Send a key to the agent…  (Esc to close)"
    : "Type a command…  (Esc to close)";
  paletteFilter();
  $("palette").style.display = "flex"; $("palette-q").focus();
}
function closePalette(){
  if(!P.open) return;
  P.open = false; $("palette").style.display = "none";
}
$("palette").addEventListener("click", e => { if(e.target === $("palette")) closePalette(); });
$("palette-q").addEventListener("input", paletteFilter);
document.addEventListener("keydown", (e) => {
  if(e.ctrlKey && !e.altKey && !e.metaKey && e.key.toLowerCase() === "k"){
    e.preventDefault();
    if(P.open){ closePalette(); } else { openPalette(e.shiftKey ? "keys" : "commands"); }
    return;
  }
  if(!P.open) return;
  if(e.key === "Escape"){ e.preventDefault(); closePalette(); }
  else if(e.key === "ArrowDown"){ e.preventDefault(); paletteMove(1); }
  else if(e.key === "ArrowUp"){ e.preventDefault(); paletteMove(-1); }
  else if(e.key === "Enter"){ e.preventDefault(); paletteRun(P.items[P.i]); }
});
