"use strict";
async function newSession(id){
  const a = agentById(id); if(!a) return;
  if(!await confirmDlg({title:"Start a clean conversation?", message:`${a.name||a.agent_id}'s current context will be discarded.`, ok:"Start new session", danger:true})) return;
  try { const r = await op("agent_new_session", {target:id});
        toast(r.confirmed ? "New session started" : "Sent; not confirmed yet", r.confirmed ? "ok" : "info"); }
  catch(err){ flash(err.message); }
}
async function stopTurn(id){
  try { const r = await op("agent_interrupt", {target:id});
        toast(r.interrupted ? "Interrupted" : "Nothing to interrupt", r.interrupted ? "ok" : "info"); }
  catch(err){ flash(err.message); }
}

/* ---------- Live view: render SGR (ANSI) safely ----------
 * We never trust the stream: text is appended as text nodes, so a stray "<"
 * stays literal (see the anti-XSS test). We are deliberately conservative and
 * only act on SGR sequence parameters we understand; anything else is dropped.
 */
function ansiLine(line, parent){
  const re = /\x1b\[([0-9;]*)m/g;
  let pos = 0, m, state = {fg:null, bg:null, bold:false, dim:false, ital:false, under:false, strike:false};
  const emit = (txt) => {
    if(!txt) return;
    const cls = ["ansi-" + (state.fg || "d"), state.bg ? "ansi-bg-" + state.bg : null,
      state.bold ? "b" : null, state.dim ? "dim" : null, state.ital ? "i" : null,
      state.under ? "u" : null, state.strike ? "s" : null].filter(Boolean).join(" ");
    const span = el("span", {className:cls});
    span.textContent = txt;                 // text node: no HTML injection
    parent.append(span);
  };
  while((m = re.exec(line))){
    emit(line.slice(pos, m.index));
    pos = re.lastIndex;
    const parts = m[1] === "" ? [0] : m[1].split(";").map(Number);
    for(let i = 0; i < parts.length; i++){
      const n = parts[i];
      if(n === 0){ state = {fg:null, bg:null, bold:false, dim:false, ital:false, under:false, strike:false}; }
      else if(n === 1) state.bold = true;
      else if(n === 2) state.dim = true;
      else if(n === 3) state.ital = true;
      else if(n === 4) state.under = true;
      else if(n === 7) { const f = state.fg; state.fg = state.bg; state.bg = f; }
      else if(n === 9) state.strike = true;
      else if(n >= 30 && n <= 37) state.fg = "c" + (n - 30);
      else if(n >= 90 && n <= 97) state.fg = "b" + (n - 90);
      else if(n >= 40 && n <= 47) state.bg = "c" + (n - 40);
      else if(n >= 100 && n <= 107) state.bg = "b" + (n - 100);
      else if(n === 39) state.fg = null;
      else if(n === 49) state.bg = null;
      else if(n === 38 && parts[i+1] === 5){ state.fg = "256-" + parts[i+2]; i += 2; }
      else if(n === 38 && parts[i+1] === 2){ state.fg = "rgb-" + parts.slice(i+2, i+5).join("-"); i += 4; }
      else if(n === 48 && parts[i+1] === 5){ state.bg = "256-" + parts[i+2]; i += 2; }
      else if(n === 48 && parts[i+1] === 2){ state.bg = "rgb-" + parts.slice(i+2, i+5).join("-"); i += 4; }
    }
  }
  emit(line.slice(pos));
}
function renderTerm(){
  const e = $("term");
  const out = S.output[S.selected] || "";
  if(!out){ e.replaceChildren(document.createTextNode("(no output yet)")); }
  else {
    const frag = document.createDocumentFragment();
    const lines = out.split("\n");
    lines.forEach((ln, i) => { ansiLine(ln.replace(/\r$/, ""), frag); if(i < lines.length - 1) frag.append(document.createTextNode("\n")); });
    e.replaceChildren(frag);
  }
  if(S.follow) e.scrollTop = e.scrollHeight;
}

/* ---------- permission prompts / questions raised by the agent's TUI ---------- */
function renderAsks(){
  const box = $("asks"), a = agentById(S.selected);
  const items = (a && a.interactions) || [];
  const key = items.map(i=>i.id).join("|");
  if(box.dataset.key === key) return;            // unchanged: keep what the user is typing/selecting
  box.dataset.key = key;
  box.replaceChildren(...items.map(askCard));
}
async function answer(item, card, ans){
  card.querySelectorAll("button").forEach(b=>b.disabled = true);
  const err = card.querySelector(".err"); err.textContent = "";
  try { await op("interaction_respond", {id:item.id, answer:ans}); card.style.opacity = "0.5"; }
  catch(e){ err.textContent = e.message; card.querySelectorAll("button").forEach(b=>b.disabled = false); }
}
function askCard(item){
  const card = el("div", {className:"ask", role:"alertdialog"});
  const err = el("div", {className:"err", role:"alert"});
  if(item.kind === "permission"){
    card.append(el("div", {className:"who"}, ic("shield","sm"), "PERMISSION REQUESTED"),
                el("div", {className:"t", textContent:item.title}));
    if(item.detail) card.append(el("pre", {textContent:item.detail}));
    if((item.patterns||[]).length) card.append(el("pre", {textContent:item.patterns.join("\n")}));
    const why = el("input", {type:"text", placeholder:"Optional note for the agent (sent with Deny)"});
    const btn = (label, reply, cls) => el("button", {className:"btn " + (cls||""), type:"button", textContent:label,
      title: reply==="always" && (item.always||[]).length ? "Always allow: " + item.always.join(", ") : "",
      onclick: ()=>answer(item, card, reply==="reject" && why.value.trim() ? {reply, message:why.value.trim()} : {reply})});
    const choices = item.choices || ["once", "always", "reject"];
    card.append(why, el("div", {className:"btns"},
      btn("Allow once", "once", "primary"),
      ...(choices.includes("always") ? [btn("Always allow", "always")] : []),
      btn("Deny", "reject", "danger"), ...terminalBtn(item, card)), err);
    return card;
  }
  card.append(el("div", {className:"who"}, ic("msg","sm"), "THE AGENT ASKS"));
  const getters = (item.questions||[]).map((q, qi)=>{
    const wrap = el("div", {className:"q"});
    if(q.header) wrap.append(el("div", {className:"h", textContent:q.header}));
    wrap.append(el("div", {className:"t", textContent:q.question}));
    const name = `${item.id}-${qi}`, type = q.multiple ? "checkbox" : "radio";
    const boxes = (q.options||[]).map(o=>{
      const inp = el("input", {type, name, value:o.label});
      wrap.append(el("label", {}, inp, el("span", {}, o.label, o.description ? el("span", {className:"d", textContent:" — " + o.description}) : "")));
      return inp;
    });
    const other = q.custom ? el("input", {type:"text", placeholder: q.multiple ? "other (added to the selection)" : "or type your own answer"}) : null;
    if(other){ wrap.append(other);
      if(!q.multiple) other.oninput = ()=>{ if(other.value) boxes.forEach(b=>b.checked = false); }; }
    card.append(wrap);
    return ()=>{
      const picked = boxes.filter(b=>b.checked).map(b=>b.value);
      const typed = other && other.value.trim();
      return q.multiple ? picked.concat(typed ? [typed] : []) : (typed ? [typed] : picked);
    };
  });
  card.append(el("div", {className:"btns"},
    el("button", {className:"btn primary", type:"button", textContent:"Answer", onclick: ()=>{
      const answers = getters.map(g=>g());
      if(answers.some(x=>!x.length)){ err.textContent = "Answer every question first."; return; }
      answer(item, card, {answers}); }}),
    el("button", {className:"btn danger", type:"button", textContent:"Dismiss", onclick: ()=>answer(item, card, {reject:true})}),
    ...terminalBtn(item, card)), err);
  return card;
}
/* Claude is paused on these: hand the dialog back to its TUI instead of answering here. */
function terminalBtn(item, card){
  return item.terminal ? [el("button", {className:"btn", type:"button", textContent:"Answer in terminal",
    title:"Let the agent show its own dialog (it also does so if nobody answers here in time)",
    onclick: ()=>answer(item, card, {terminal:true})})] : [];
}

/* ---------- Processes: the commands the agent runs (live output, Ctrl-C / terminate / kill) ---------- */
const fmtAge = s => s == null ? "—" : s < 60 ? `${Math.round(s)}s` : s < 3600 ? `${Math.floor(s/60)}m ${Math.round(s%60)}s` : `${Math.floor(s/3600)}h ${Math.floor(s%3600/60)}m`;
const PSTATE = {R:"running", S:"sleeping", D:"waiting on disk/IO", T:"stopped", Z:"zombie", I:"idle"};
async function refreshProcs(){
  const id = S.selected; if(S.view!=="proc" || !id) return;
  let r;
  try { r = await op("agent_processes", {target:id}); }
  catch(e){ if(S.selected===id && S.view==="proc") $("procs-in").replaceChildren(el("div", {className:"note"}, ic("alert"), e.message)); return; }
  if(S.selected!==id || S.view!=="proc") return;
  S.proc.data = r; renderProcs();
}
function procRef(sh){ return sh.output ? (sh.output.kind==="file" ? {task:sh.output.task} : {call:sh.output.call}) : null; }
function renderProcs(){
  const r = S.proc.data, box = $("procs-in"); if(!r) return;
  // The selected command ended: keep showing its output among the finished ones.
  if(S.proc.sel && S.proc.sel.startsWith("pid:") && !r.shells.some(sh => "pid:"+sh.pid === S.proc.sel))
    S.proc.sel = S.proc.ref && S.proc.ref.task ? "task:" + S.proc.ref.task : null;
  const keep = box.querySelector("pre.out"), keepTop = keep ? keep.scrollTop : 0, keepEnd = keep ? nearBottom(keep) : true;
  const kids = [];
  kids.push(r.shells.length
    ? el("div", {className:"note"}, ic("terminal","sm"), `${r.shells.length} command${r.shells.length===1?"":"s"} running. Their input is closed: they can be watched and stopped, not typed into.`)
    : el("div", {className:"state-empty"}, ic("check","lg"), "No command running right now."));
  r.shells.forEach(sh => kids.push(shellCard(sh, true)));
  if((r.finished||[]).length){
    kids.push(el("h4", {textContent:"Finished commands · latest first"}));
    r.finished.forEach(t => kids.push(finishedRow(t)));
  }
  if((r.others||[]).length){
    const d = el("details", {}, el("summary", {className:"pm", textContent:`${r.others.length} other process(es) of the agent (MCP servers, helpers…)`}));
    r.others.forEach(sh => d.append(shellCard(sh, false)));
    kids.push(d);
  }
  box.replaceChildren(...kids);
  const out = box.querySelector("pre.out");
  if(out){ out.textContent = S.proc.out || ""; out.scrollTop = keepEnd ? out.scrollHeight : keepTop; }
}
function shellCard(sh, isShell){
  const key = "pid:" + sh.pid, sel = S.proc.sel === key;
  const stale = isShell && sh.last_output_ago != null && sh.last_output_ago > 60;
  const card = el("div", {className:"sh" + (sel ? " sel" : "") + (stale ? " stale" : ""), role:"button", tabIndex:0});
  const sub = (sh.children||[]).map(c => c.command.split(" ")[0].split("/").pop()).slice(0,6).join(", ");
  card.append(el("div", {className:"top"}, el("div", {className:"cmd"}, sh.command),
    stale ? el("span", {className:"pill s-working", title:"No output for over a minute — it may be stuck"}, ic("alert","sm"), "quiet") : el("span", {className:"pill s-ready"}, dot("working"), "running")));
  card.append(el("div", {className:"pm"}, `pid ${sh.pid} · ${PSTATE[sh.state]||sh.state} · running ${fmtAge(sh.elapsed)} · cpu ${sh.cpu}s`
    + (sh.last_output_ago != null ? ` · last output ${fmtAge(sh.last_output_ago)} ago` : "")
    + (sub ? ` · running: ${sub}` : "")));
  const sig = (name, label, cls) => el("button", {className:"btn sm " + (cls||""), type:"button", textContent:label, onclick:(e)=>{ e.stopPropagation(); signalProc(sh, name); }});
  card.append(el("div", {className:"btns"}, sig("INT", "Ctrl-C"), sig("TERM", "Terminate", "danger"), sig("KILL", "Kill", "danger")));
  const ref = procRef(sh);
  if(ref) card.onclick = ()=>{ S.proc.sel = sel ? null : key; S.proc.ref = sel ? null : ref; S.proc.out = ""; renderProcs(); if(!sel) refreshProcOut(); };
  if(sel) card.append(el("pre", {className:"out", textContent:"loading output…", onclick:e=>e.stopPropagation()}));
  return card;
}
function finishedRow(t){
  const key = "task:" + t.task, sel = S.proc.sel === key;
  const row = el("div", {className:"sh" + (sel ? " sel" : ""), role:"button", tabIndex:0},
    el("div", {className:"pm", style:"margin:0"}, `${t.task} · ${t.size} bytes · ended ${new Date(t.mtime*1000).toLocaleTimeString()}`));
  row.onclick = ()=>{ S.proc.sel = sel ? null : key; S.proc.ref = sel ? null : {task:t.task}; S.proc.out = ""; renderProcs(); if(!sel) refreshProcOut(); };
  if(sel) row.append(el("pre", {className:"out", textContent:"loading output…", onclick:e=>e.stopPropagation()}));
  return row;
}
async function refreshProcOut(){
  const id = S.selected, ref = S.proc && S.proc.ref; if(S.view!=="proc" || !id || !ref) return;
  try {
    const r = await op("agent_process_output", {target:id, ...ref});
    if(S.selected!==id || S.view!=="proc" || S.proc.ref !== ref) return;
    S.proc.out = (r.truncated ? "… (earlier output cut)\n" : "") + (r.output || (r.available ? "(no output yet)" : "(output not available — the command may have finished)"));
    const out = $("procs").querySelector("pre.out");
    if(out && out.textContent !== S.proc.out){ const end = nearBottom(out); out.textContent = S.proc.out; if(end) out.scrollTop = out.scrollHeight; }
  } catch(e){}
}
async function signalProc(sh, name){
  const what = {INT:"Send Ctrl-C to", TERM:"Terminate", KILL:"Kill"}[name];
  if(!await confirmDlg({title:`${what} this command?`, message:`It and everything it started.\n\n${sh.command.slice(0,300)}`, ok:what.replace(" to",""), danger:name!=="INT"})) return;
  try { const r = await op("agent_process_signal", {target:S.selected, pid:sh.pid, signal:name});
        toast(`${name} → ${r.signalled.length} process(es)` + (r.abort_if_stuck ? " · if OpenCode does not notice, its turn is interrupted in a few seconds" : ""), "ok"); }
  catch(e){ flash(e.message); }
  setTimeout(refreshProcs, 600);
}
setInterval(()=>{ if(S.view==="proc" && S.selected && !document.hidden) refreshProcs(); }, 2000);
setInterval(()=>{ if(S.view==="proc" && S.selected && !document.hidden) refreshProcOut(); }, 1000);

/* ---------- message log ---------- */
function renderActivity(){
  const msgs = (S.state?.messages)||[];
  $("log-n").textContent = msgs.length;
  const marks = {acknowledged:["✓✓","ok"], injected:["✓","ok"], queued:["…","pend"], failed:["✗","bad"]};
  $("activity").replaceChildren(...(msgs.slice(-12).map(m=>{
    const [mark, cls] = marks[m.status] || (m.delivered ? ["✓","ok"] : ["✗","bad"]);
    const s = m.sender_name||m.sender, r = m.recipient_name||m.recipient;
    return el("div", {className:"row", title:m.status||""}, el("span", {className:"mk " + cls}, mark),
      el("span", {className:"who"}, `${s} → ${r}`), el("span", {className:"body"}, m.body));
  })) .concat(msgs.length ? [] : [el("div", {className:"none"}, "(no messages)")]));
}
function toggleLog(){ const l = $("log"); l.classList.toggle("open"); store.set("at.log", l.classList.contains("open") ? "1" : "0"); }
$("log-head").onclick = toggleLog;
$("log-head").onkeydown = e => { if(e.key==="Enter"||e.key===" "){ e.preventDefault(); toggleLog(); } };
if(store.get("at.log") === "1") $("log").classList.add("open");

