"use strict";
/* ---------- command palette (Ctrl+K) ---------- */
const P = { open:false, items:[], all:[], i:0 };
function paletteCommands(){
  const cmds = [
    {label:"New agent", icon:"plus", run:()=>openNewAgent()},
    {label:"New team", icon:"users", run:()=>newTeam()},
  ];
  const attn = inboxItems().length;
  if(attn) cmds.push({label:`Needs your answer (${attn})`, icon:"shield", run:()=>openInbox()});
  cmds.push({label:"Mission control", icon:"activity", run:()=>openMission(null)});
  (S.state?.teams||[]).forEach(t => cmds.push({label:`Mission control: ${t.name}`, icon:"activity", run:()=>openMission(t.team_id)}));
  cmds.push({label:"Timeline", icon:"list", run:()=>openTimeline()});
  cmds.push({label:"Cost & usage", icon:"shield", run:()=>openUsage()});
  cmds.push({label:"Clean up (temp, backups, releases)", icon:"trash", run:()=>openClean()});
  cmds.push({label:"Archived agents", icon:"terminal", run:()=>openArchived()});
  cmds.push({label:"Setup wizard", icon:"plus", run:()=>openOnboarding()});
  if(S.selected){
    cmds.push({label:"Go to Conversation", icon:"msg", run:()=>setView("hist")});
    cmds.push({label:"Go to Live", icon:"activity", run:()=>setView("live")});
    cmds.push({label:"Go to Processes", icon:"terminal", run:()=>setView("proc")});
  }
  (S.state?.agents || []).forEach(a => {
    cmds.push({label:`Switch to ${a.name||a.agent_id}`, icon:"terminal", run:()=>{ select(a.agent_id); }});
    cmds.push({label:`Send to ${a.name||a.agent_id}…`, icon:"send", run:()=>{ select(a.agent_id); $("input").focus(); }});
    if(a.state === "working") cmds.push({label:`Stop ${a.name||a.agent_id}`, icon:"stop", danger:true, run:()=>stopTurn(a.agent_id)});
  });
  return cmds;
}
function paletteFilter(){
  const q = $("palette-q").value.trim().toLowerCase();
  P.items = q ? P.all.filter(c => c.label.toLowerCase().includes(q)) : P.all.slice();
  P.i = 0;
  renderPalette();
}
function renderPalette(){
  const list = $("palette-list");
  if(!P.items.length){ list.replaceChildren(el("div", {className:"palette-empty"}, "No matching commands")); return; }
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
function openPalette(){
  if(P.open) return;
  P.open = true; P.all = paletteCommands();
  $("palette-q").value = ""; paletteFilter();
  $("palette").style.display = "flex"; $("palette-q").focus();
}
function closePalette(){
  if(!P.open) return;
  P.open = false; $("palette").style.display = "none";
}
$("palette").addEventListener("click", e => { if(e.target === $("palette")) closePalette(); });
$("palette-q").addEventListener("input", paletteFilter);
document.addEventListener("keydown", (e) => {
  if(e.ctrlKey && !e.shiftKey && !e.altKey && !e.metaKey && (e.key === "k" || e.key === "K")){ e.preventDefault(); P.open ? closePalette() : openPalette(); return; }
  if(!P.open) return;
  if(e.key === "Escape"){ e.preventDefault(); closePalette(); }
  else if(e.key === "ArrowDown"){ e.preventDefault(); paletteMove(1); }
  else if(e.key === "ArrowUp"){ e.preventDefault(); paletteMove(-1); }
  else if(e.key === "Enter"){ e.preventDefault(); paletteRun(P.items[P.i]); }
});
