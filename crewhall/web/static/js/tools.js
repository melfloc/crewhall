"use strict";
/* ---------- composer tools: uploads, model switch, slash commands, export,
   global search, quick actions and @-mentions ---------- */

/* ============================ attachments ============================ */
S.attach = S.attach || [];

function fmtBytes(n){
  n = Number(n) || 0;
  if(n < 1024) return n + " B";
  if(n < 1024*1024) return (n/1024).toFixed(n < 10240 ? 1 : 0) + " KB";
  return (n/1024/1024).toFixed(1) + " MB";
}
function hasAttachments(){ return S.attach.length > 0; }
function clearAttachments(){ S.attach = []; renderAttachments(); }
function addAttachment(rec){ S.attach.push(rec); renderAttachments(); }
function renderAttachments(){
  const tray = $("attachTray"); if(!tray) return;
  if(!S.attach.length){ tray.classList.add("init-hidden"); tray.replaceChildren(); return; }
  tray.classList.remove("init-hidden");
  tray.replaceChildren(...S.attach.map((a, i) => el("span", {className:"attach-chip", title:a.path},
    ic(a.kind === "image" ? "image" : "file", "sm"),
    el("span", {className:"nm"}, a.name),
    el("span", {className:"sz"}, fmtBytes(a.size)),
    el("button", {className:"x", type:"button", "aria-label":"Remove attachment",
      onclick:() => { S.attach.splice(i, 1); renderAttachments(); }}, ic("x", "sm")))));
}
/* The message actually sent: the prompt plus the absolute path of each file,
   on its own line (quoted when it has spaces) so the agent can read it. */
function withAttachments(text){
  if(!S.attach.length) return text;
  const paths = S.attach.map(a => /\s/.test(a.path) ? JSON.stringify(a.path) : a.path);
  const head = String(text || "").replace(/\s+$/, "");
  return (head ? head + "\n\n" : "") + paths.join("\n");
}

async function uploadFiles(fileList, storage){
  const files = [...(fileList || [])]; if(!files.length) return;
  const btn = $("attachBtn"); if(btn) btn.classList.add("loading");
  try {
    for(const f of files){
      const r = await fetch("/api/upload?mode=" + encodeURIComponent(storage || ""), {
        method:"POST", credentials:"same-origin",
        headers:{"Content-Type": f.type || "application/octet-stream",
                 "X-Filename": encodeURIComponent(f.name)},
        body: f });
      const j = await r.json().catch(() => ({ok:false, error:r.statusText}));
      if(!j.ok) throw new Error(j.error || "upload failed");
      addAttachment({path:j.path, name:j.name, size:j.size, mode:j.mode,
                     kind:(f.type || "").startsWith("image/") ? "image" : "file"});
    }
  } catch(e){ flash(e.message); }
  finally { if(btn) btn.classList.remove("loading"); }
}

$("attachBtn").onclick = (e) => {
  e.stopPropagation();
  openMenu($("attachBtn"), [
    {label:"Attach a file…", icon:"file", run:() => { S.uploadMode = ""; $("fileInput").click(); }},
    {label:"Attach to permanent storage…", icon:"folder", run:() => { S.uploadMode = "permanent"; $("fileInput").click(); }},
    "-",
    {label:"Insert a server path…", icon:"folder", run:insertServerPath},
  ]);
};
$("fileInput").onchange = (e) => { uploadFiles(e.target.files, S.uploadMode); e.target.value = ""; };
async function insertServerPath(){
  const r = await promptDlg({title:"Insert a path", ok:"Insert",
    fields:[{key:"path", label:"Absolute path on the server", required:true, mono:true, path:true,
             placeholder:"/home/me/project/file.txt"}]});
  if(!r) return;
  addAttachment({path:r.path, name:r.path.split("/").pop() || r.path, size:0, mode:"path", kind:"file"});
}
/* Drop files on the composer to upload them. */
(function(){
  const wrap = $("composer-wrap"); if(!wrap) return;
  const stop = (e) => { e.preventDefault(); e.stopPropagation(); };
  ["dragenter", "dragover"].forEach(ev => wrap.addEventListener(ev, e => { stop(e); wrap.classList.add("dragging"); }));
  ["dragleave", "dragend"].forEach(ev => wrap.addEventListener(ev, e => {
    stop(e); if(e.target === wrap) wrap.classList.remove("dragging"); }));
  wrap.addEventListener("drop", e => {
    stop(e); wrap.classList.remove("dragging");
    const dt = e.dataTransfer; if(dt && dt.files && dt.files.length) uploadFiles(dt.files, "");
  });
})();
/* Paste a screenshot straight from the clipboard. */
$("input").addEventListener("paste", (e) => {
  const items = (e.clipboardData || {}).items || [];
  const files = [];
  for(const it of items) if(it.kind === "file"){ const f = it.getAsFile(); if(f) files.push(f); }
  if(files.length){ e.preventDefault(); uploadFiles(files, ""); }
});

/* ============================ model switch ============================ */
S.modelInfo = S.modelInfo || {};
async function loadModels(id){
  const hit = S.modelInfo[id];
  if(hit && Date.now() - hit.at < 5000) return hit.info;
  let info;
  try {
    const r = await op("agent_models", {target:id});
    info = (r && r.models) ? r.models : {mode:"direct", models:[], current:null};
  } catch(e){ info = {mode:"direct", models:[], current:null}; }
  S.modelInfo[id] = {at:Date.now(), info};
  return info;
}
async function renderModelCtl(a){
  const ctl = $("modelCtl"); if(!ctl) return;
  if(!a){ ctl.replaceChildren(); ctl.dataset.sig = ""; return; }
  const info = await loadModels(a.agent_id);
  const sig = [a.agent_id, info.mode, info.current, (info.models || []).join(",")].join("|");
  if(ctl.dataset.sig === sig) return;
  ctl.dataset.sig = sig;
  if(info.mode === "picker"){
    ctl.replaceChildren(el("button", {className:"btn sm model-btn", type:"button",
      title:"Open the agent's own model picker", onclick:() => setAgentModel(null)},
      ic("activity", "sm"), el("span", {}, info.current || "model")));
  } else {
    const opts = (info.models || []).map(m => typeof m === "string" ? {value:m, label:m} : m);
    if(info.current && !opts.some(o => o.value === info.current))
      opts.unshift({value:info.current, label:info.current});
    if(!opts.length) opts.push({value:"default", label:"default"});
    ctl.replaceChildren(el("select", {className:"select sm model-sel", title:"Change the model",
      onchange:(e) => {
        const v = e.target.value;
        if(v === "__custom"){ e.target.value = info.current || opts[0].value; openModelPicker(a); return; }
        setAgentModel(v);
      }},
      ...opts.map(o => el("option", {value:o.value, textContent:o.label, selected:o.value === info.current})),
      el("option", {value:"__custom", textContent:"Custom…"})));
  }
}
async function setAgentModel(model){
  const a = agentById(S.selected); if(!a) return;
  try {
    await op("agent_set_model", {target:a.agent_id, model:model || null});
    S.modelInfo[a.agent_id] = null;
    toast(model ? `Switching model to ${model}` : "Model picker opened", "ok");
  } catch(e){ flash(e.message); }
}
async function openModelPicker(a){
  const info = await loadModels(a.agent_id);
  if(info.mode === "picker") return setAgentModel(null);
  const names = (info.models || []).map(m => typeof m === "string" ? m : m.label).slice(0, 12);
  const r = await promptDlg({title:"Change model", sub:`${a.name || a.agent_id} · ${a.kind}`, ok:"Switch",
    fields:[{key:"model", label:"Model", value:info.current || "", required:true, mono:true,
             placeholder:"e.g. sonnet", hint:names.join(", ")}]});
  if(r) setAgentModel(r.model);
}

/* ============================ slash commands ============================ */
const SLASH = {
  claude: [["/help","Help"],["/model","Change model"],["/compact","Compact context"],
    ["/context","Show context"],["/clear","Clear conversation"],["/plan","Plan mode"],
    ["/permissions","Permissions"],["/memory","Edit memory"],["/init","Create CLAUDE.md"],
    ["/mcp","MCP servers"],["/status","Status"],["/config","Settings"],["/cost","Cost"],
    ["/resume","Resume session"],["/exit","Exit"]],
  opencode: [["/help","Help"],["/models","Change model"],["/agents","Switch agent"],
    ["/sessions","Sessions"],["/new","New session"],["/compact","Compact"],
    ["/undo","Undo"],["/redo","Redo"],["/share","Share"],["/export","Export"],
    ["/init","Create AGENTS.md"],["/themes","Theme"],["/thinking","Thinking"],
    ["/connect","Connect provider"],["/exit","Exit"]],
  codex: [["/help","Help"],["/model","Change model"],["/plan","Plan mode"],
    ["/review","Review changes"],["/diff","Show diff"],["/permissions","Permissions"],
    ["/status","Status"],["/compact","Compact"],["/init","Create AGENTS.md"],
    ["/mcp","MCP tools"],["/agent","Switch agent"],["/ps","Background terminals"],["/exit","Exit"]],
};
function openSlash(anchor){
  const a = agentById(S.selected); if(!a) return;
  const cmds = SLASH[a.kind];
  if(!cmds){ toast("No known slash commands for this agent", "info"); return; }
  const items = cmds.map(([cmd, label]) => ({label:`${cmd} — ${label}`, icon:"terminal",
    run:() => insertTemplate(cmd)}));
  openMenu(anchor, items);
}
$("cmdBtn").onclick = (e) => { e.stopPropagation(); openSlash($("cmdBtn")); };

/* ============================ quick actions ============================ */
function agentKey(id, key){
  return op("agent_key", {target:id, key}).catch(e => flash(e.message));
}
function quickActionsBtn(a){
  const btn = el("button", {className:"btn", type:"button", id:"agent-more", title:"Quick actions"},
    ic("bolt", "sm"), el("span", {className:"lbl"}, "More"));
  btn.onclick = (e) => { e.stopPropagation(); openQuickActions(btn, a); };
  return btn;
}
async function restartAgent(id){
  try { await op("agent_restart", {target:id}); toast("Agent starting…", "ok"); }
  catch(e){ flash(e.message); }
}
function openQuickActions(anchor, a){
  const items = [];
  if(a.state === "exited" || a.state === "error")
    items.push({label:"Start the agent", icon:"refresh", run:() => restartAgent(a.agent_id)});
  if(a.state === "working") items.push({label:"Interrupt the turn", icon:"stop", danger:true, run:() => stopTurn(a.agent_id)});
  items.push({label:"New session", icon:"refresh", run:() => newSession(a.agent_id)});
  if(a.kind === "claude") items.push({label:"Cycle permission mode (Shift+Tab)", icon:"shield",
    run:() => agentKey(a.agent_id, "SHIFT_TAB")});
  items.push({label:"Change model…", icon:"activity", run:() => openModelPicker(a)});
  items.push("-");
  items.push({label:"Export conversation (Markdown)", icon:"file", run:() => exportConversation("md")});
  items.push({label:"Export conversation (JSON)", icon:"file", run:() => exportConversation("json")});
  items.push({label:"Search all conversations…", icon:"search", run:openGlobalSearch});
  openMenu(anchor, items);
}

/* ============================ export conversation ============================ */
function conversationMarkdown(a, msgs){
  const out = [`# ${a.name || a.agent_id} (${a.kind})`, `_Exported ${new Date().toLocaleString()}_`, ""];
  msgs.forEach(m => {
    out.push(`## ${m.role === "user" ? "You" : "Agent"}${m.at ? " — " + new Date(m.at).toLocaleString() : ""}`);
    out.push("", m.text || "", "");
  });
  return out.join("\n");
}
async function exportConversation(fmt){
  const a = agentById(S.selected); if(!a) return;
  try {
    const r = await op("agent_history", {target:a.agent_id, limit:2000});
    if(!r.available){ toast("This agent has no readable conversation", "info"); return; }
    const text = fmt === "json" ? JSON.stringify(r.messages, null, 2) : conversationMarkdown(a, r.messages);
    const blob = new Blob([text], {type: fmt === "json" ? "application/json" : "text/markdown"});
    const url = URL.createObjectURL(blob);
    const link = el("a", {href:url, download:`${(a.name || a.agent_id).replace(/[^\w.-]+/g, "-")}-conversation.${fmt}`});
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    toast("Conversation exported", "ok");
  } catch(e){ flash(e.message); }
}

/* ============================ global search ============================ */
function openGlobalSearch(){
  const input = el("input", {className:"input", placeholder:"Search every agent's conversation…",
    autocomplete:"off", spellcheck:false});
  const results = el("div", {className:"search-results"});
  let closer = null, seq = 0, timer = null;
  const run = async () => {
    const q = input.value.trim(), mine = ++seq;
    if(q.length < 2){ results.replaceChildren(el("div", {className:"hint"}, "Type at least 2 characters.")); return; }
    results.replaceChildren(el("div", {className:"skel"}));
    try {
      const r = await op("conversation_search", {query:q, limit:50});
      if(mine !== seq) return;
      if(!r.matches.length){ results.replaceChildren(el("div", {className:"hint"}, "No matches.")); return; }
      results.replaceChildren(...r.matches.map(m => el("button", {className:"search-hit", type:"button",
        onclick:() => { if(closer) closer(undefined); jumpToMatch(m); }},
        el("div", {className:"who"}, el("span", {className:"nm"}, m.name), el("span", {className:"chip"}, m.kind),
          el("span", {className:"muted"}, m.role || "")),
        el("div", {className:"snip"}, m.snippet))));
    } catch(e){ if(mine === seq) results.replaceChildren(el("div", {className:"note"}, ic("alert"), " ", e.message)); }
  };
  input.oninput = () => { clearTimeout(timer); timer = setTimeout(run, 250); };
  openDlg(close => { closer = close;
    return el("form", {onsubmit:e => e.preventDefault()},
      el("h3", {}, "Search conversations"),
      el("p", {className:"sub"}, "Substring search across every agent's readable conversation."),
      el("div", {className:"field"}, input),
      results,
      el("div", {className:"dlg-actions"},
        el("button", {className:"btn", type:"button", onclick:() => close(undefined)}, "Close")));
  });
  setTimeout(() => input.focus(), 30);
}
function jumpToMatch(m){
  select(m.agent_id);
  setView("hist");
  const id = "msg-" + m.index;
  let tries = 0;
  const go = () => {
    const n = document.getElementById(id);
    if(n){ n.scrollIntoView({block:"center"}); n.classList.add("flash-hit");
      setTimeout(() => n.classList.remove("flash-hit"), 1800); }
    else if(tries++ < 12) setTimeout(go, 400);
  };
  setTimeout(go, 500);
}

/* ============================ @-mentions (workspace paths) ============================ */
function attachMentions(){
  const b = $("input"); if(!b) return;
  let pop = null, entries = [], active = -1, ticket = 0, timer = null, at = -1;
  const close = () => { if(pop){ pop.remove(); pop = null; } active = -1; entries = []; };
  const place = () => { if(!pop) return; const r = b.getBoundingClientRect();
    Object.assign(pop.style, {left:r.left + "px", top:Math.max(8, r.top - 220) + "px", width:Math.min(520, r.width) + "px"}); };
  const paint = () => {
    if(!pop) return;
    if(!entries.length){ close(); return; }
    pop.replaceChildren(...entries.map((e, i) => el("div", {className:"path-opt" + (i === active ? " on" : ""),
      onmousedown:(ev) => { ev.preventDefault(); accept(i); }}, e.path)));
    place();
  };
  const accept = (i) => {
    const e = entries[i]; if(!e) return;
    const pos = b.selectionStart;
    b.value = b.value.slice(0, at) + e.path + b.value.slice(pos);
    composerGrow(); b.focus();
    const np = at + e.path.length; b.setSelectionRange(np, np);
    close();
  };
  const refresh = async (prefix) => {
    const mine = ++ticket;
    try { const r = await op("fs_complete", {prefix}); if(mine !== ticket) return;
      entries = (r.entries || []).slice(0, 8); active = -1;
      if(!pop){ pop = el("div", {className:"path-pop", role:"listbox"}); document.body.append(pop); }
      paint();
    } catch(e){ close(); }
  };
  b.addEventListener("input", () => {
    const pos = b.selectionStart, before = b.value.slice(0, pos);
    const m = before.match(/(?:^|\s)@([^\s@]*)$/);
    if(!m){ close(); return; }
    at = pos - m[1].length - 1;
    clearTimeout(timer); timer = setTimeout(() => refresh(m[1]), 140);
  });
  b.addEventListener("keydown", (e) => {
    if(!pop) return;
    if(e.key === "ArrowDown"){ e.preventDefault(); active = (active + 1) % entries.length; paint(); }
    else if(e.key === "ArrowUp"){ e.preventDefault(); active = (active - 1 + entries.length) % entries.length; paint(); }
    else if((e.key === "Enter" || e.key === "Tab") && active >= 0){ e.preventDefault(); e.stopPropagation(); accept(active); }
    else if(e.key === "Escape"){ e.stopPropagation(); close(); }
  }, true);
  b.addEventListener("blur", () => setTimeout(close, 150));
  addEventListener("resize", () => { if(pop) place(); });
}
attachMentions();
