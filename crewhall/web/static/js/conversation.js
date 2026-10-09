"use strict";
function select(id){ S.selected = id; S.selectedTerm = null;
  if(S.termView){ S.termView.dispose(); S.termView = null; }
  if(typeof clearAttachments === "function") clearAttachments();
  S.follow = true;
  $("follow").className="follow on"; $("follow").textContent="● follow";
  sendFocus(id);               // server pushes this agent's transcript right away
  const sel = agentById(id);
  setView(!sel || sel.history ? "hist" : "live");   // unknown yet (just created) -> Conversation
  render(); renderTerm(); if(!S.output[id]) refreshOutput(id); }

/* ---------- conversation view: one scrollable container, no buttons ---------- */
function setView(v){
  S.view = v;
  [["hist","tab-hist"],["live","tab-live"],["proc","tab-proc"]].forEach(([k, id]) => {
    $(id).className = "tab" + (v===k ? " on" : ""); $(id).setAttribute("aria-selected", v===k); });
  $("histSearch").style.display = v === "hist" ? "inline-flex" : "none";
  layout();
  if(v==="hist") openHistory();
  if(v==="proc"){ S.proc = {sel:null}; $("procs-in").replaceChildren(el("div", {className:"skel"}), el("div", {className:"skel"})); refreshProcs(); }
}
function inline(parent, text){
  text.split(/(`[^`\n]+`|\*\*[^*\n]+\*\*)/).forEach(part=>{
    if(!part) return;
    let n;
    if(part.startsWith("`") && part.endsWith("`") && part.length>2){ n = document.createElement("code"); n.textContent = part.slice(1,-1); }
    else if(part.startsWith("**") && part.endsWith("**") && part.length>4){ n = document.createElement("strong"); n.textContent = part.slice(2,-2); }
    else n = document.createTextNode(part);
    parent.append(n);
  });
}
/* Tiny syntax highlighter (no external library): keywords, strings, numbers,
 * comments and function calls. Output is text nodes only, so it is XSS-safe. */
const HL_KEYWORDS = new Set(("def class function return if else elif for while import from as try except finally " +
  "with lambda yield await async not and or in is None True False self pass break continue global raise " +
  "const let var new this typeof instanceof of null undefined true false public private static void " +
  "final int str bool float list dict async await yield match case").split(" "));
const HL_RE = /(\/\/[^\n]*|#[^\n]*|\/\*[\s\S]*?\*\/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|\b\d[\d_.]*\b|\b[A-Za-z_$][\w$]*\b)/g;
function highlightInto(code, text){
  let last = 0, m;
  while((m = HL_RE.exec(text))){
    if(m.index > last) code.append(document.createTextNode(text.slice(last, m.index)));
    const tok = m[0];
    let cls;
    if(tok.startsWith("//") || tok.startsWith("#") || tok.startsWith("/*")) cls = "hl-c";
    else if('"\'`'.includes(tok[0])) cls = "hl-s";
    else if(/^\d/.test(tok)) cls = "hl-n";
    else if(HL_KEYWORDS.has(tok)) cls = "hl-k";
    else if(/^\s*\(/.test(text.slice(m.index + tok.length))) cls = "hl-f";
    if(cls){ const s = document.createElement("span"); s.className = cls; s.textContent = tok; code.append(s); }
    else code.append(document.createTextNode(tok));
    last = HL_RE.lastIndex;
  }
  if(last < text.length) code.append(document.createTextNode(text.slice(last)));
}
function codePre(lang, body){
  const pre = document.createElement("pre"), code = document.createElement("code");
  if(lang) code.className = "lang-" + lang;
  highlightInto(code, body); pre.append(code); return pre;
}
function elCodeBlock(seg){
  const lines = seg.split("\n");
  if(lines.length < 2 || !lines.every(l => !l.trim() || /^ {4,}\S/.test(l))) return null;
  return codePre("", lines.map(l => l.replace(/^ {4}/, "")).join("\n"));
}
function mdNode(text){
  const root = document.createDocumentFragment();
  text.split(/```/).forEach((seg, i)=>{
    if(i%2===1){                       // fenced code
      const m = seg.match(/^([^\n]*)\n/);
      const lang = (m && /^[A-Za-z0-9+#-]+$/.test(m[1].trim())) ? m[1].trim().toLowerCase() : "";
      root.append(codePre(lang, seg.replace(/^[^\n]*\n/, "").replace(/\n$/, ""))); return; }
    let para = null, list = null, listType = null;
    const code = elCodeBlock(seg);     // indented (4-space) code fallback
    if(code){ root.append(code); return; }
    const flush = ()=>{ para = null; };
    seg.split("\n").forEach(line=>{
      let m;
      if(!line.trim()){ flush(); list = null; return; }
      if((m = line.match(/^(#{1,4})\s+(.*)/))){ flush(); list=null;
        const h = document.createElement("h"+m[1].length); inline(h, m[2]); root.append(h); return; }
      if((m = line.match(/^\s*([-*]|\d+\.)\s+(.*)/))){ flush();
        const type = /\d/.test(m[1]) ? "ol" : "ul";
        if(!list || listType!==type){ list = document.createElement(type); listType = type; root.append(list); }
        const li = document.createElement("li"); inline(li, m[2]); list.append(li); return; }
      list = null;
      if(!para){ para = document.createElement("p"); root.append(para); } else para.append(document.createElement("br"));
      inline(para, line);
    });
  });
  return root;
}
const DIFF_LINE = /^([+-])(?!\+\+)(?!--)/;
function diffNode(text){
  const lines = text.split("\n");
  if(!lines.some(l => DIFF_LINE.test(l) || /^@@ /.test(l) || /^diff --git/.test(l))) return null;
  const box = el("div", {className:"diff"});
  lines.forEach(l => {
    let cls = "ctx";
    if(/^@@ /.test(l)) cls = "hunk";
    else if(/^diff --git|^index |^--- |^\+\+\+ /.test(l)) cls = "meta";
    else if(/^\+/.test(l)) cls = "add";
    else if(/^-/.test(l)) cls = "del";
    box.append(el("div", {className:"diff-line " + cls, textContent:l || " "}));
  });
  return box;
}
function jsonPretty(s){
  try { return JSON.stringify(JSON.parse(s), null, 2); } catch(e){ return null; }
}
function toolArgs(b){
  const obj = jsonPretty(b.text); if(!obj) return null;
  try { return JSON.parse(obj); } catch(e){ return null; }
}
function blockNode(b){
  if(b.type==="text"){ const d = document.createElement("div"); d.append(mdNode(b.text)); return d; }
  const isTool = b.type==="tool_use", name = b.name || "tool";
  const det = document.createElement("details"), sum = document.createElement("summary");
  sum.textContent = isTool ? "🔧 " + name : "↳ result";
  det.append(sum);
  if(isTool){
    const args = toolArgs(b);
    // A short one-line summary of what the tool is doing.
    let brief = null;
    if(args){
      brief = args.command || args.file_path || args.path || args.pattern || args.query || args.description || args.url || null;
    }
    if(brief) det.append(el("div", {className:"tool-summary", textContent:String(brief).slice(0, 300)}));
    const body = args ? args.content || args.new_string || args.command || (typeof args === "object" ? "" : String(args)) : "";
    const diff = body ? diffNode(String(body)) : null;
    if(diff) det.append(diff);
    else {
      const raw = jsonPretty(b.text) || b.text;
      det.append(el("pre", {}, codeEl(raw)));
    }
  } else {
    const diff = diffNode(b.text);
    if(diff) det.append(diff);
    else det.append(el("pre", {}, codeEl(b.text + (b.truncated ? "\n… (truncated)" : ""))));
  }
  return det;
}
function codeEl(text){
  const code = document.createElement("code");
  highlightInto(code, text); return code;
}
function fmtWhen(iso){
  const d = new Date(iso); if(isNaN(d)) return "";
  return d.toDateString() === new Date().toDateString() ? d.toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"}) : d.toLocaleString([], {dateStyle:"medium", timeStyle:"short"});
}
function histNode(m){
  const d = document.createElement("div"); d.className = "msg " + m.role;
  const w = document.createElement("div"); w.className = "who";
  const toolOnly = m.blocks && m.blocks.length && m.blocks.every(x=>x.type==="tool_result");
  const role = document.createElement("span"); role.className = "role";
  role.textContent = toolOnly ? "tool" : m.role==="user" ? "you / incoming" : "agent";
  w.append(role);
  if(m.at){ const t = document.createElement("time"); t.textContent = fmtWhen(m.at); t.title = new Date(m.at).toLocaleString(); w.append(t); }
  const link = document.createElement("button"); link.className = "msg-link"; link.type = "button";
  link.title = "Copy a link to this message"; link.textContent = "#";
  link.onclick = () => {
    const id = "msg-" + (m._idx != null ? m._idx : "");
    const url = location.origin + location.pathname + "#" + id;
    copyText(url, "Message link copied");
  };
  w.append(link);
  const b = document.createElement("div"); b.className = "body";
  (m.blocks && m.blocks.length ? m.blocks : [{type:"text", text:m.text}]).forEach(x=>b.append(blockNode(x)));
  d.append(w, b); return d;
}
/* Right after sending: show the message at once and poll fast until the real history has it. */
function afterSend(text){
  const id = S.selected; if(S.view!=="hist" || !id) return;
  const h = ensureHist(id);
  if(h.pending) h.pending.node.remove();
  const node = histNode({role:"user", text, blocks:[{type:"text", text}], at:new Date().toISOString()});
  node.classList.add("pending");
  $("hist-list").append(node); h.pending = {node, total:h.total};
  h.follow = true; $("hist").scrollTop = $("hist").scrollHeight; paintStatus(h);
  [400, 1000, 2000, 3500, 6000].forEach(ms => setTimeout(()=>{
    if(S.selected===id && S.view==="hist") fetchLatest(); }, ms));
}
function ensureHist(id){
  return S.hist[id] || (S.hist[id] = {nodes:new Map(), sigs:new Map(), order:[], min:null, total:0,
                                      follow:true, loading:false});
}
const msgSig = m => (m.at||"") + ":" + m.text.length + ":" + (m.blocks ? m.blocks.length : 0);
const nearBottom = box => box.scrollHeight - box.scrollTop - box.clientHeight < 60;

function upsert(h, idx, m){
  const sig = msgSig(m);
  if(h.sigs.get(idx) === sig) return false;
  m._idx = idx;
  const node = histNode(m), old = h.nodes.get(idx);
  node.id = "msg-" + idx;
  h.sigs.set(idx, sig); h.nodes.set(idx, node);
  const list = $("hist-list");
  if(old){ old.replaceWith(node); return true; }
  // keep DOM order == index order
  let pos = h.order.length;
  while(pos > 0 && h.order[pos-1] > idx) pos--;
  h.order.splice(pos, 0, idx);
  const next = h.nodes.get(h.order[pos+1]);
  if(next && next.parentNode === list) list.insertBefore(node, next); else list.append(node);
  return true;
}
function paintStatus(h){
  const a = agentById(S.selected) || {state:"starting"};
  const st = $("hist-status");
  $("hist-top").textContent = h.min === 0 && h.total ? "— start of conversation —" : (h.loading ? "loading earlier messages…" : "");
  let mode = "text", t = "";
  if(!h.total && !h.pending) t = "No messages yet — send a prompt below.";
  else if(a.state==="working") mode = "work";
  else if(["unknown","starting","exited","error"].includes(a.state))
    t = `agent is ${a.state}${a.state==="exited"||a.state==="error" ? "" : " — if it seems stuck, check the Live tab"}`;
  if(mode === "work"){
    if(st.dataset.mode !== "work"){
      st.className = "work"; st.dataset.mode = "work";
      st.replaceChildren(el("span", {className:"typing"}, el("i"), el("i"), el("i")), "Agent is working…",
        a.agent_id ? timerSpan(a) : null, el("span", {className:"muted", style:"font-weight:400"}, "Ctrl+C to stop"));
    }
  } else { st.className = ""; st.dataset.mode = "text"; st.textContent = t; }
  $("hist-note").textContent = h.total ? `${h.total} messages` : "";
}
function openHistory(){
  const id = S.selected; if(!id) return;
  const h = ensureHist(id), list = $("hist-list");
  $("hist-status").dataset.mode = "";
  list.replaceChildren(...h.order.map(i => h.nodes.get(i)), ...(h.pending ? [h.pending.node] : []));
  h.follow = true; paintStatus(h);
  const box = $("hist");
  if(location.hash && document.querySelector(location.hash)) document.querySelector(location.hash).scrollIntoView({block:"center"});
  else box.scrollTop = box.scrollHeight;
  fetchLatest();
}
async function fetchLatest(){
  const id = S.selected; if(!id) return;
  const h = ensureHist(id), box = $("hist");
  try {
    const r = await op("agent_history", {target:id, limit:200});
    if(S.selected!==id || S.view!=="hist") return;
    if(h.conv === undefined) h.conv = r.conversation_id;
    else if(r.conversation_id !== h.conv){                         // /clear, /new, /resume
      S.hist[id] = null; const n = ensureHist(id); n.conv = r.conversation_id; return openHistory(); }
    if(r.available){
      if(r.total < h.total){ S.hist[id] = null; return openHistory(); }   // conversation restarted
      let changed = false;
      r.messages.forEach((m,i)=>{ if(upsert(h, r.start+i, m)) changed = true; });
      if(h.min === null || r.start < h.min) h.min = r.start;
      h.total = r.total;
      if(h.pending && h.total > h.pending.total){ h.pending.node.remove(); h.pending = null; changed = true; }
      if(changed && h.follow) box.scrollTop = box.scrollHeight;
    }
    paintStatus(h);
  } catch(e){}
}
async function loadOlder(){
  const id = S.selected; if(!id) return;
  const h = ensureHist(id), box = $("hist");
  if(h.loading || h.min === null || h.min <= 0) return;
  h.loading = true; paintStatus(h);
  try {
    const r = await op("agent_history", {target:id, limit:200, before:h.min});
    if(S.selected!==id || S.view!=="hist" || !r.available || r.conversation_id !== h.conv) return;
    const prevH = box.scrollHeight, prevTop = box.scrollTop;
    r.messages.forEach((m,i)=>upsert(h, r.start+i, m));
    h.min = r.start;
    box.scrollTop = prevTop + (box.scrollHeight - prevH);       // keep the reading position
  } catch(e){} finally { h.loading = false; paintStatus(h); }
}
function updateJump(){
  const h = S.hist[S.selected];
  const show = !!S.selected && ((S.view==="hist" && h && !h.follow) || (S.view==="live" && !S.follow));
  $("jump").classList.toggle("show", show);
}
/* ---------- in-conversation search ---------- */
function clearMarks(){
  document.querySelectorAll("#hist-list mark").forEach(m => {
    const parent = m.parentNode; if(!parent) return;
    parent.replaceChild(document.createTextNode(m.textContent), m);
    parent.normalize();
  });
}
function findInHistory(q){
  clearMarks();
  const note = $("hist-find");
  q = q.trim().toLowerCase();
  if(!q){ note.textContent = ""; return; }
  const marks = [];
  const walker = document.createTreeWalker($("hist-list"), NodeFilter.SHOW_TEXT);
  const targets = [];
  let n;
  while((n = walker.nextNode())) if(n.nodeValue && n.nodeValue.toLowerCase().includes(q)) targets.push(n);
  targets.forEach(node => {
    const text = node.nodeValue, low = text.toLowerCase();
    let i = 0, last = 0;
    const frag = document.createDocumentFragment();
    while((i = low.indexOf(q, last)) !== -1){
      if(i > last) frag.append(document.createTextNode(text.slice(last, i)));
      const mark = document.createElement("mark"); mark.textContent = text.slice(i, i + q.length);
      frag.append(mark); marks.push(mark); last = i + q.length;
    }
    if(last < text.length) frag.append(document.createTextNode(text.slice(last)));
    node.parentNode.replaceChild(frag, node);
  });
  note.textContent = marks.length ? `${marks.length} match${marks.length === 1 ? "" : "es"}` : "no matches";
  if(marks.length){ const box = $("hist"); box.scrollTop = Math.max(0, marks[marks.length - 1].offsetTop - box.clientHeight / 2); }
}
$("hist-q").addEventListener("input", ()=> findInHistory($("hist-q").value));
$("hist-q").addEventListener("keydown", e => { if(e.key === "Escape"){ $("hist-q").value = ""; findInHistory(""); $("hist-q").blur(); } });
$("hist-qClear").onclick = ()=>{ $("hist-q").value = ""; findInHistory(""); $("hist-q").focus(); };
document.addEventListener("keydown", (e) => {
  if((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && (e.key === "f" || e.key === "F")
     && S.selected && S.view === "hist"){
    e.preventDefault(); $("histSearch").style.display = "inline-flex"; $("hist-q").focus(); $("hist-q").select();
  }
});
addEventListener("hashchange", () => { const t = document.querySelector(location.hash); if(t) t.scrollIntoView({block:"center"}); });

$("jump").onclick = ()=>{
  if(S.view==="hist"){ const h = ensureHist(S.selected); h.follow = true; $("hist").scrollTop = $("hist").scrollHeight; }
  else { S.follow = true; setFollowUi(); renderTerm(); }
  updateJump();
};
$("hist").addEventListener("scroll", ()=>{
  const box = $("hist"), h = S.hist[S.selected]; if(!h) return;
  h.follow = nearBottom(box);                                  // scrolled up -> stop following
  updateJump();
  if(box.scrollTop < 120) loadOlder();                         // reached the top -> earlier messages
});
setInterval(()=>{ if(S.view==="hist" && S.selected && !document.hidden) fetchLatest(); }, 1500);
$("tab-live").onclick = ()=>setView("live");
$("tab-hist").onclick = ()=>setView("hist");
$("tab-proc").onclick = ()=>setView("proc");

async function refreshOutput(id){
  try { const r = await op("agent_transcript", {target:id, max_lines:200});
    S.output[id] = r.output || ""; if(S.selected===id) renderTerm(); } catch(e){}
}

// Conversation view: Ctrl+C stops the running turn. Document level, because the
// input is disabled while the agent works. Normal copy is kept when text is selected.
document.addEventListener("keydown", async (e)=>{
  if(!(e.ctrlKey && !e.shiftKey && !e.altKey && !e.metaKey && (e.key==="c" || e.key==="C"))) return;
  if(S.view!=="hist" || !S.selected) return;
  const inp = $("input");
  if(document.activeElement === inp && inp.selectionStart !== inp.selectionEnd) return;
  if(String(window.getSelection()).length) return;
  e.preventDefault();
  try { const r = await op("agent_interrupt", {target:S.selected});
        toast(r.interrupted ? "Interrupted" : "Nothing to interrupt", r.interrupted ? "ok" : "info"); } catch(err){ flash(err.message); }
});
// Global shortcuts: "/" search · Alt+↑/↓ previous / next agent
document.addEventListener("keydown", (e)=>{
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "");
  if(e.key === "/" && !typing && !e.ctrlKey && !e.metaKey){ e.preventDefault(); closeDrawer(); $("shell").classList.add("drawer"); $("q").focus(); return; }
  if(e.key === "Escape"){ closeMenu(); closeDrawer(); }
  if(e.altKey && (e.key === "ArrowUp" || e.key === "ArrowDown") && S.state){
    const list = [...document.querySelectorAll("#nav .agent-row")].map(r => r.dataset.agent);
    if(!list.length) return; e.preventDefault();
    const i = list.indexOf(S.selected), n = e.key === "ArrowDown" ? Math.min(list.length-1, i+1) : Math.max(0, i < 0 ? 0 : i-1);
    select(list[n]);
  }
});

async function submitInput(){
  const box = $("input"), text = box.value;
  const atts = (typeof hasAttachments === "function") && hasAttachments();
  if((!text.trim() && !atts) || !S.selected || box.disabled) return;
  const full = typeof withAttachments === "function" ? withAttachments(text) : text;
  const btn = $("send"); box.value = ""; composerGrow(); btn.classList.add("loading");
  pushHistory(full);
  try { await op("agent_write", {target:S.selected, text:full}); await op("agent_key", {target:S.selected, key:"ENTER"});
        if(typeof clearAttachments === "function") clearAttachments();
        afterSend(full); }
  catch(err){ flash(err.message); if(!box.value){ box.value = text; composerGrow(); } }
  finally { btn.classList.remove("loading"); }
}
$("input").addEventListener("input", composerGrow);
$("input").addEventListener("keydown", (e)=>{
  if(e.isComposing) return;
  const box = $("input");
  if(e.key === "Enter"){
    if(e.shiftKey) return;                       // Shift+Enter inserts a newline
    e.preventDefault(); submitInput(); return;
  }
  if(e.key === "ArrowUp" && (S.histIdx != null || (box.value.indexOf("\n") === -1 && box.selectionStart === 0))){
    if(historyNav(-1)) e.preventDefault();
  } else if(e.key === "ArrowDown" && S.histIdx != null){
    if(historyNav(1)) e.preventDefault();
  }
});
$("send").onclick = submitInput;
$("keys").querySelectorAll("button").forEach(b=> b.onclick = async ()=>{
  if(!S.selected) return;
  try { await op("agent_key", {target:S.selected, key:b.dataset.key}); } catch(e){ flash(e.message); }
});
function setFollowUi(){ $("follow").className = "follow" + (S.follow ? " on" : "");
  $("follow").textContent = S.follow ? "● follow" : "❚❚ paused"; updateJump(); }
$("follow").onclick = ()=>{ S.follow=!S.follow; setFollowUi(); renderTerm(); };
$("follow").onkeydown = e => { if(e.key==="Enter"||e.key===" "){ e.preventDefault(); $("follow").click(); } };
$("copyTerm").onclick = ()=> copyText(S.output[S.selected] || "", "Output copied");
$("term").addEventListener("scroll", ()=>{ const t=$("term");
  const atBottom = t.scrollHeight - t.scrollTop - t.clientHeight < 8;
  if(!atBottom && S.follow){ S.follow=false; setFollowUi(); } });


/* Keep only the latest activity (tool / sub-agent / result) expanded: when a new
   one appears, the one we auto-opened closes and the new one opens. Blocks the user
   opened or closed by hand are left alone. */
function syncLatestOpen(){
  const list = $("hist-list"); if(!list) return;
  const all = [...list.querySelectorAll("details")], keep = new Set();
  const last = all[all.length - 1];
  if(last){ keep.add(last);
    // a result is shown with the call that produced it
    if(last.firstChild && /^↳/.test(last.firstChild.textContent||"") && all.length > 1) keep.add(all[all.length - 2]); }
  all.forEach(d => {
    if(keep.has(d)){ if(!d.dataset.auto && !d.dataset.manual){ d.dataset.auto = "1"; d.open = true; } }
    else if(d.dataset.auto){ delete d.dataset.auto; if(!d.dataset.manual) d.open = false; }
  });
}
(function(){
  const list = $("hist-list"); let queued = false;
  const run = () => { queued = false; syncLatestOpen(); };
  new MutationObserver(() => { if(!queued){ queued = true; requestAnimationFrame(run); } })
    .observe(list, {childList:true, subtree:true});
  // a toggle that did not come from us is the user's choice: respect it
  list.addEventListener("click", e => { const s = e.target.closest && e.target.closest("summary");
    if(s && s.parentNode.tagName === "DETAILS") s.parentNode.dataset.manual = "1"; }, true);
})();
