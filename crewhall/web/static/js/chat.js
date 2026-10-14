"use strict";
/* Chat mode: a full alternative layout (conversation sidebar · full-view
   conversation · artifact panel) that reuses the existing engine. A chat is a
   real crewhall agent; messages come from agent_history and are rendered with
   the same histNode() the Conversation view uses. Artifacts live in the
   conversation's outputs/ and are served by the web layer. */

const CHAT = {
  open: false,
  list: [],
  active: null,          // conversation id
  info: null,            // {id,title,agent_id,outputs,inputs,agent,...}
  msgs: [],
  sig: "",
  follow: true,
  attach: [],
  q: "",
  poll: null,
  office: null,          // {editor, path}
  lastArtifacts: 0,
  layout: store.get("at.chat.layout") === "document" ? "document" : "chat",
};

/* Crew activity strip (handoffs/requests of a chat's Cowork team). */
const CREW = {data:null, sig:"", tick:0, all:false};

const chatBody = () => document.body.classList;

/* ---------- top-level interfaces: Cowork (agents) and Chat (conversations) ---------- */
function currentInterface(){ return store.get("at.mode") === "chat" ? "chat" : "cowork"; }
function renderModeSwitch(){
  const mode = currentInterface();
  document.querySelectorAll("#modeSwitch button").forEach(b => {
    const on = b.dataset.mode === mode;
    b.classList.toggle("on", on);
    b.setAttribute("aria-pressed", on);
  });
}
function setInterface(mode){
  mode = mode === "chat" ? "chat" : "cowork";
  store.set("at.mode", mode);
  if(mode === "chat") chatOn(); else chatOff();
  renderModeSwitch();
}
function chatOn(){
  CHAT.open = true;
  chatBody().add("chat-on");
  $("chat").classList.remove("init-hidden");
  chatApplyLayout();
  chatRenderHead();
  chatLoadList();
  chatLoadOffice();
  chatStartPoll();
}
function chatOff(){
  CHAT.open = false;
  chatBody().remove("chat-on");
  $("chat").classList.add("init-hidden");
  chatStopPoll();
  chatCloseOffice();
}

/* ---------- in-chat layout: "Chat" (conversation first) or "Document" ---------- */
function chatApplyLayout(){
  const doc = CHAT.layout === "document";
  $("chat").classList.toggle("layout-document", doc);
  $("chat").classList.toggle("layout-chat", !doc);
  if(doc) chatSetArtifacts(true);
}
function chatSetLayout(layout){
  CHAT.layout = layout === "document" ? "document" : "chat";
  store.set("at.chat.layout", CHAT.layout);
  chatApplyLayout();
  chatRenderHead();
}
function chatLayoutToggle(){
  const mk = (m, label, icon) => el("button", {className: CHAT.layout === m ? "on" : "", type:"button",
    "aria-pressed": CHAT.layout === m, onclick: () => chatSetLayout(m)}, ic(icon, "sm"), label);
  return el("div", {className:"chat-layout", role:"group", "aria-label":"Layout"},
    mk("chat", "Chat", "msg"), mk("document", "Document", "columns"));
}

/* ---------- sidebar ---------- */
async function chatLoadList(){
  try {
    const r = await op("conversation_list");
    CHAT.list = r.conversations || [];
  } catch(e){ CHAT.list = []; }
  chatRenderList();
}
function chatRenderList(){
  const list = $("chat-list");
  const q = CHAT.q.trim().toLowerCase();
  const items = CHAT.list.filter(c => !q || (c.title||"").toLowerCase().includes(q));
  if(!items.length){
    list.replaceChildren(el("div", {className:"chat-empty"},
      CHAT.list.length ? "No chat matches." : "No chats yet. Create one to start."));
    return;
  }
  list.replaceChildren(...items.map(c => {
    const on = c.id === CHAT.active;
    const st = c.agent ? c.agent.state : "exited";
    const row = el("button", {className:"chat-row" + (on ? " on" : ""), type:"button",
      onclick:()=>chatSelect(c.id)},
      el("span", {className:`dot s-${st}`, "aria-hidden":"true"}),
      el("span", {className:"chat-row-mid"},
        el("span", {className:"chat-row-title", textContent:c.title || "Chat"}),
        el("span", {className:"chat-row-sub", textContent:
          `${c.agent ? (c.agent.name || c.agent.agent_id) : "no agent"} · ${c.outputs||0} artifact${(c.outputs||0)===1?"":"s"}`})),
      el("span", {className:"chat-row-menu", role:"button", tabIndex:0, title:"Delete chat",
        onclick:(e)=>{ e.stopPropagation(); chatDelete(c); },
        onkeydown:(e)=>{ if(e.key==="Enter"){ e.stopPropagation(); chatDelete(c); } }}, ic("trash","sm")));
    return row;
  }));
}

async function chatNew(){
  let meta = { harnesses:[{kind:"opencode"}], defaults:{} };
  try { meta = await op("meta_info"); } catch(e){}
  const kinds = (meta.harnesses||[]).map(h => h.kind);
  let crews = [];
  try { const r = await op("team_list"); crews = (r.teams||[]).filter(t => !t.host); } catch(e){}
  const fields = [
    {key:"title", label:"Name", value:"New chat", required:true},
    {key:"kind", label:"Provider", type:"select",
      options:(kinds.length ? kinds : ["opencode"]).map(k => ({value:k, label:k})),
      value:(meta.defaults||{}).kind || kinds[0] || "opencode"},
    {key:"crew", label:"Crew", type:"select", value:"",
      options:[{value:"", label:"Solo (no team)"},
               ...crews.map(t => ({value:t.name, label:t.name}))]},
  ];
  const out = await promptDlg({title:"New chat",
    sub:"Starts an agent isolated in this conversation's own folder. A crew lets it delegate to a Cowork team.",
    fields, ok:"Create"});
  if(!out) return;
  try {
    const conv = await op("conversation_create", {title:out.title, team:out.crew || null});
    const cid = conv.conversation.id;
    const res = await op("agent_create", {kind:out.kind, name:out.title, conversation:cid});
    CHAT.list.unshift({...conv.conversation, agent:res.agent, outputs:0, inputs:0});
    chatRenderList();
    await chatSelect(cid);
  } catch(e){ flash(e.message); }
}

/* Set/clear a chat's crew from the chat head. */
function chatCrewMenu(anchor){
  op("team_list").then(r => {
    const crews = (r.teams||[]).filter(t => !t.host);
    const items = [{label:"Solo (no team)", icon:"users", run:()=>chatSetCrew(null)}];
    crews.forEach(t => items.push({label:t.name, icon:"users", run:()=>chatSetCrew(t.name)}));
    if(!crews.length) items.push({label:"(no local teams yet)", run:()=>{}});
    openMenu(anchor, items);
  }).catch(e => flash(e.message));
}
async function chatSetCrew(team){
  try {
    const r = await op("conversation_set_team", {id:CHAT.active, team});
    if(CHAT.info) CHAT.info.team = r.conversation.team;
    toast(team ? `Crew: ${team}` : "Crew removed", "ok");
    chatRenderHead();
    CREW.sig = ""; chatLoadActivity(true);
  } catch(e){ flash(e.message); }
}

async function chatSelect(id){
  CHAT.active = id; CHAT.msgs = []; CHAT.sig = "";
  chatRenderList();
  try {
    const r = await op("conversation_info", {id});
    CHAT.info = r.conversation;
  } catch(e){ CHAT.info = null; flash(e.message); }
  chatRenderHead();
  chatRenderAsks();
  chatLoadHistory(true);
  chatLoadArtifacts();
  CREW.sig = ""; chatLoadActivity(true);
}

async function chatDelete(c){
  if(!await confirmDlg({title:"Delete this chat?", message:`“${c.title||"Chat"}” and its agent will be removed. Files are kept unless you delete them.`, ok:"Delete", danger:true})) return;
  try {
    if(c.agent_id){ try { await op("agent_stop", {target:c.agent_id}); } catch(e){} }
    await op("conversation_delete", {id:c.id});
    CHAT.list = CHAT.list.filter(x => x.id !== c.id);
    if(CHAT.active === c.id){ CHAT.active = null; CHAT.info = null; chatRenderHead(); $("chat-feed").replaceChildren(); chatRenderArtifacts(); }
    chatRenderList();
  } catch(e){ flash(e.message); }
}

/* ---------- head ---------- */
function chatRenderHead(){
  const head = $("chat-head");
  if(!CHAT.info){
    head.replaceChildren(el("div", {className:"chat-head-main"},
      el("div", {className:"chat-head-txt"}, el("div", {className:"chat-head-title"}, "Select a chat")),
      el("div", {className:"chat-head-actions"}, chatLayoutToggle())));
    $("chat-input").disabled = true; $("chatSend").disabled = true;
    return;
  }
  const a = CHAT.info.agent || (CHAT.info.agent_id ? agentById(CHAT.info.agent_id) : null);
  const st = a ? (a.state||"unknown") : "exited";
  const acts = [];
  if(a && (st === "exited" || st === "error"))
    acts.push(el("button", {className:"btn sm primary", type:"button", onclick:()=>chatRestart()}, ic("refresh","sm"), "Start"));
  if(CHAT.office) acts.push(el("button", {className:"btn sm", type:"button", onclick:()=>chatCloseOffice()}, "Close editor"));
  acts.push(el("button", {className:"btn sm", type:"button", title:"Set the Cowork crew for this chat",
    onclick:(e)=>{ e.stopPropagation(); chatCrewMenu(e.currentTarget); }}, ic("users","sm"),
    CHAT.info.team ? "Crew" : "Solo"));
  acts.push(el("a", {className:"btn sm", title:"Export this chat (transcript + files) as a ZIP",
    href:`/api/conversation/export?id=${encodeURIComponent(CHAT.info.id)}`, download:""},
    ic("down","sm"), "Export"));
  head.replaceChildren(
    el("div", {className:"chat-head-main"},
      el("span", {className:`avatar ${st}`, style:`--h:${hue((a&&a.kind)||CHAT.info.title)}`}, (CHAT.info.title||"?").trim().charAt(0)),
      el("div", {className:"chat-head-txt"},
        el("div", {className:"chat-head-title", textContent:CHAT.info.title || "Chat"}),
        el("div", {className:"chat-head-sub"},
          a ? el("span", {className:`pill s-${st}`}, dot(st, actOf(a).kind), stateLabel(st)) : el("span", {className:"pill s-exited"}, "no agent"),
          a && a.kind ? el("span", {className:"chip mono"}, a.kind) : null,
          CHAT.info.team ? el("span", {className:"chip crew"}, ic("users","sm"), "crew") : null,
          a && a.activity && a.activity.model ? el("span", {className:"chip mono"}, a.activity.model) : null)),
      el("div", {className:"chat-head-actions"}, chatLayoutToggle(), ...acts)));
  const working = a && st === "working";
  // Exited/error chats are still sendable: sending revives the agent.
  const canSend = a && ["ready", "waiting_input", "exited", "error"].includes(st);
  $("chat-input").disabled = !canSend;
  $("chatSend").disabled = !canSend;
  $("chat-input").placeholder = !a ? "No agent — create a chat"
    : working ? "Agent working… (Ctrl+C to stop)"
    : st === "starting" ? "Agent starting…"
    : (st === "exited" || st === "error") ? "Send a message to start the agent again…"
    : "Message the agent…";
}

/* ---------- pending interactions (permissions / questions) in chat mode ----------
   Reuses the exact cards Cowork shows (agent.js askCard); answering posts the same
   interaction_respond op, so the chat can clear an agent that is waiting on you.

   Chat agents are deliberately hidden from the Cowork snapshot, so `agentById`
   (which reads S.state.agents) never finds them: the summary that carries the
   pending interactions comes from `conversation_info.agent` instead. */
function chatAgent(){
  if(!CHAT.info) return null;
  return CHAT.info.agent || agentById(CHAT.info.agent_id) || null;
}
function chatRenderAsks(){
  const box = $("chat-asks"); if(!box) return;
  const a = chatAgent();
  const items = (a && a.interactions) || [];
  const key = items.map(i=>i.id).join("|");
  if(box.dataset.key === key) return;
  box.dataset.key = key;
  if(!items.length){ box.classList.add("init-hidden"); box.replaceChildren(); return; }
  box.classList.remove("init-hidden");
  box.replaceChildren(
    el("div", {className:"chat-asks-head"}, ic("shield","sm"),
      items.length === 1 ? "The agent needs your answer" : `${items.length} answers pending`),
    ...items.map(askCard));
}

/* Make sure the chat's agent is usable, reviving it if it exited. */
async function chatEnsureAgent(){
  const info = CHAT.info; if(!info || !info.agent_id) return false;
  let a = info.agent || agentById(info.agent_id);
  if(a && (a.state === "ready" || a.state === "waiting_input")) return true;
  if(a && a.state === "working") return false;
  try { await op("agent_restart", {target:info.agent_id}); }
  catch(e){ flash(e.message); return false; }
  try { const r = await op("conversation_info", {id:CHAT.active}); CHAT.info = r.conversation; chatRenderHead(); } catch(e){}
  a = CHAT.info.agent;
  return !!(a && (a.state === "ready" || a.state === "waiting_input"));
}

async function chatRestart(){
  const a = CHAT.info && CHAT.info.agent;
  if(!a) return;
  try { await op("agent_restart", {target:a.agent_id}); toast("Agent starting", "ok"); }
  catch(e){ flash(e.message); }
  setTimeout(()=>{ chatLoadList(); const id = CHAT.active; if(id) chatSelect(id); }, 800);
}

/* ---------- conversation ---------- */
const chatMsgSig = m => (m.at||"") + ":" + (m.text||"").length + ":" + ((m.blocks||[]).length);

async function chatLoadHistory(force){
  const id = CHAT.active, info = CHAT.info;
  if(!id || !info || !info.agent_id) { if(info) chatRenderFeed(); return; }
  let r;
  try { r = await op("agent_history", {target:info.agent_id, limit:200}); }
  catch(e){ return; }
  if(CHAT.active !== id) return;
  const sig = (r.total||0) + "|" + (r.messages||[]).map(chatMsgSig).join(";");
  if(sig === CHAT.sig && !force) return;
  CHAT.sig = sig;
  CHAT.msgs = r.messages || [];
  chatRenderFeed();
}

function chatFollow(){
  if(!CHAT._follow) CHAT._follow = followBottom($("chat-feed"), {threshold:80,
    onChange:(f)=>{ CHAT.follow = f; }});
  return CHAT._follow;
}

function chatRenderFeed(){
  const feed = $("chat-feed");
  const info = CHAT.info;
  if(!info){ feed.replaceChildren(el("div", {className:"chat-empty"}, "Pick a conversation.")); return; }
  const msgs = CHAT.msgs;
  const f = chatFollow();
  const following = f.isFollowing();
  if(!msgs.length){
    feed.replaceChildren(el("div", {className:"chat-hero"},
      el("div", {className:"art"}, ic("msg","lg")),
      el("h2", {textContent:info.title || "Chat"}),
      el("p", {textContent:"Send a message to start. Files the agent writes into its outputs folder appear in the artifact panel."})));
    return;
  }
  const frag = document.createDocumentFragment();
  msgs.forEach(m => frag.append(histNode(m)));
  feed.replaceChildren(frag);
  if(following) f.toBottom();
}

async function chatSend(){
  const box = $("chat-input"), text = box.value.trim();
  if((!text && !CHAT.attach.length) || !CHAT.info || !CHAT.info.agent_id || box.disabled) return;
  const atts = CHAT.attach.slice();
  const full = atts.length ? `${text}\n\n${atts.map(a=>`Attached: ${a.path}`).join("\n")}` : text;
  box.value = ""; chatGrow(); $("chatSend").classList.add("loading");
  CHAT.attach = []; chatRenderTray();
  CHAT.msgs.push({role:"user", text:full, blocks:[{type:"text", text:full}], at:new Date().toISOString()});
  chatFollow().setFollow(true); chatRenderFeed(); chatFollow().toBottom();
  try {
    if(!await chatEnsureAgent()) throw new Error("the agent is not ready");
    await op("agent_write", {target:CHAT.info.agent_id, text:full});
    await op("agent_key", {target:CHAT.info.agent_id, key:"ENTER"});
    [500, 1200, 2500, 5000].forEach(ms => setTimeout(()=>chatLoadHistory(), ms));
  } catch(e){ flash(e.message); if(!box.value){ box.value = text; chatGrow(); } }
  finally { $("chatSend").classList.remove("loading"); }
}

function chatGrow(){
  const t = $("chat-input");
  t.style.height = "auto";
  t.style.height = Math.min(220, t.scrollHeight) + "px";
}

/* ---------- attachments ---------- */
async function chatAddFiles(files){
  if(!CHAT.active || !files.length) return;
  for(const f of files){
    try {
      const r = await fetch(`/api/conversation/upload?id=${encodeURIComponent(CHAT.active)}`,
        {method:"POST", credentials:"same-origin", headers:{"X-Filename":encodeURIComponent(f.name)}, body:f});
      const j = await r.json();
      if(!j.ok){ flash(j.error || "upload failed"); continue; }
      CHAT.attach.push({name:j.name, path:j.path || j.name});
    } catch(e){ flash(e.message || "upload failed"); }
  }
  chatRenderTray();
}

function chatRenderTray(){
  const tray = $("chat-tray");
  if(!CHAT.attach.length){ tray.classList.add("init-hidden"); tray.replaceChildren(); return; }
  tray.classList.remove("init-hidden");
  tray.replaceChildren(...CHAT.attach.map((a, i) =>
    el("span", {className:"chip"}, ic("paperclip","sm"), a.name,
      el("button", {className:"x", type:"button", "aria-label":"Remove",
        onclick:()=>{ CHAT.attach.splice(i,1); chatRenderTray(); }}, "×"))));
}

/* ---------- crew activity strip ----------
   Shows the handoffs/requests the chat's Cowork team is working on, so the user
   sees the team "behind" the chat without switching to Cowork. Tolerates
   system:* senders (OnlyOffice comments routed to the team). */
function crewName(id, name){
  if(!id) return "";
  if(String(id).startsWith("system:")) return String(id).slice(7);
  // The chat's own lead is not in the Cowork agent list (chat agents are scoped out).
  if(CHAT.info && id === CHAT.info.agent_id){
    const lead = (CHAT.list.find(c => c.id === CHAT.active) || {}).agent;
    return (lead && lead.name) || name || "lead";
  }
  const a = (typeof agentById === "function") && agentById(id);
  return (a && a.name) || name || id;
}
async function chatLoadActivity(force){
  const id = CHAT.active;
  if(!id || !CHAT.info || !CHAT.info.team){ CREW.data = null; chatRenderActivity(null); return; }
  let r;
  try { r = await op("conversation_activity", {id, limit:20, all_team: !!CREW.all}); }
  catch(e){ return; }
  if(CHAT.active !== id) return;
  const sig = (r.messages||[]).length + ":" + (r.requests||[]).length +
    ":" + (r.messages||[]).map(m=>m.status).join(",");
  if(sig === CREW.sig && !force) return;
  CREW.sig = sig; CREW.data = r;
  chatRenderActivity(r);
}
function chatRenderActivity(data){
  const box = $("chat-crew"); if(!box) return;
  const msgs = (data && data.messages) || [];
  const reqs = (data && data.requests) || [];
  if(!CHAT.info || !CHAT.info.team){
    box.classList.add("init-hidden"); box.replaceChildren(); return;
  }
  box.classList.remove("init-hidden");
  if(!msgs.length && !reqs.length){
    box.replaceChildren(el("div", {className:"chat-empty", role:"status"},
      CREW.all ? "No team activity yet." : "No crew activity in this chat yet."));
    return;
  }
  const row = (who, status, body) => el("div", {className:"crew-row"},
    el("span", {className:"crew-who", textContent:who}),
    el("span", {className:"crew-status", textContent:status || ""}),
    el("span", {className:"crew-body", title:body || "", textContent:body || ""}));
  const rows = [
    ...msgs.slice(-8).map(m => row(`${crewName(m.sender, m.sender_name)} → ${crewName(m.recipient, m.recipient_name)}`, m.status, m.body)),
    ...reqs.slice(-8).map(r => row(`${crewName(r.sender)} → ${crewName(r.recipient)}`, r.state, r.task)),
  ];
  box.replaceChildren(
    el("button", {className:"crew-toggle", type:"button", "aria-expanded":"false",
      onclick:(e)=>{ const open = box.classList.toggle("open");
                     e.currentTarget.setAttribute("aria-expanded", open ? "true" : "false"); }},
      ic("users","sm"), `Crew · ${msgs.length + reqs.length}`),
    el("button", {className:"crew-all", type:"button", "aria-pressed": CREW.all ? "true" : "false",
      title:"Show every message of the team, not only this chat's",
      onclick:()=>{ CREW.all = !CREW.all; CREW.sig = ""; chatLoadActivity(true); }},
      CREW.all ? "This chat + team" : "This chat"),
    el("div", {className:"crew-rows"}, ...rows));
}

/* ---------- artifacts ---------- */
async function chatLoadArtifacts(){
  const id = CHAT.active; if(!id) return;
  try { const r = await op("conversation_info", {id}); if(CHAT.active!==id) return; CHAT.info = r.conversation; }
  catch(e){ return; }
  chatRenderArtifacts();
  chatRenderHead();
  chatRenderAsks();
}
function fmtBytes(n){
  if(n == null) return "";
  if(n < 1024) return n + " B";
  if(n < 1048576) return (n/1024).toFixed(1) + " KB";
  return (n/1048576).toFixed(1) + " MB";
}
function chatRenderArtifacts(){
  const body = $("chat-art-body");
  const outputs = (CHAT.info && CHAT.info.outputs) || [];
  const inputs = (CHAT.info && CHAT.info.inputs) || [];
  if(!CHAT.info){ body.replaceChildren(el("div", {className:"chat-empty"}, "No chat selected.")); return; }
  const section = (title, files, isOutput) => {
    if(!files.length) return null;
    return el("div", {className:"chat-art-sec"},
      el("div", {className:"chat-art-sec-t"}, title, el("span", {className:"chip"}, String(files.length))),
      ...files.map(f => chatArtRow(f, isOutput)));
  };
  // replaceChildren() renders a null argument as the text "null": filter it out.
  body.replaceChildren(...[
    section("Outputs", outputs, true), section("Inputs", inputs, false),
    !outputs.length && !inputs.length ? el("div", {className:"chat-empty"},
      "No files yet. Ask the agent to create one — it lands in the outputs folder.") : null,
  ].filter(Boolean));
}
function chatArtRow(f, isOutput){
  const url = `/api/conversation/artifact?id=${encodeURIComponent(CHAT.active)}&path=${encodeURIComponent(f.relpath)}`;
  const dl = url + "&download=1";
  const acts = [
    isOutput && f.office ? el("button", {className:"btn sm", type:"button", onclick:()=>chatOpenOffice(f)}, "Open") : null,
    isOutput && f.office ? el("button", {className:"btn sm", type:"button", title:"Let the agent edit this live as a co-editor", onclick:()=>chatLiveEdit(f)}, ic("bolt","sm"), "Live") : null,
    f.kind === "image" || f.kind === "pdf" ? el("button", {className:"btn sm", type:"button", onclick:()=>chatPreview(f)}, "Preview") : null,
    el("a", {className:"btn sm", href:dl, download:f.name, title:"Download"}, ic("down","sm")),
    isOutput ? el("button", {className:"btn sm danger", type:"button", title:"Delete artifact",
      onclick:()=>chatDeleteArtifact(f)}, ic("trash","sm")) : null,
  ].filter(Boolean);
  return el("div", {className:"chat-art-row"},
    el("span", {className:"chat-art-ic"}, ic(f.kind === "image" ? "image" : "file","sm")),
    el("span", {className:"chat-art-mid"},
      el("span", {className:"chat-art-name", textContent:f.name, title:f.relpath}),
      el("span", {className:"chat-art-sub", textContent:
        `${f.kind} · ${fmtBytes(f.size)}${f.producer ? " · " + artifactProducer(f) : ""}`})),
    el("span", {className:"chat-art-acts"}, ...acts));
}
function artifactProducer(f){
  if(!f || !f.producer) return "";
  if(CHAT.info && f.producer === CHAT.info.agent_id) return "by the lead";
  return "by " + crewName(f.producer);
}
async function chatLiveEdit(f){
  try {
    const r = await op("office_collab_open", {id:CHAT.active, path:f.relpath});
    toast(`${r.user || "The agent"} is now editing ${f.name} live`, "ok");
  } catch(e){ flash(e.message); }
}
async function chatDeleteArtifact(f){
  if(!await confirmDlg({title:"Delete artifact?", message:f.name, ok:"Delete", danger:true})) return;
  try { await op("conversation_artifact_delete", {id:CHAT.active, path:f.relpath}); chatLoadArtifacts(); }
  catch(e){ flash(e.message); }
}
function chatPreview(f){
  chatCloseOffice();
  const url = `/api/conversation/artifact?id=${encodeURIComponent(CHAT.active)}&path=${encodeURIComponent(f.relpath)}`;
  chatSetArtifacts(true);
  const body = $("chat-art-body");
  body.replaceChildren(el("div", {className:"chat-preview"},
    el("button", {className:"btn sm", type:"button", onclick:()=>{ $("chat-artifacts").classList.remove("previewing"); chatRenderArtifacts(); }}, "← Artifacts"),
    f.kind === "image"
      ? el("img", {src:url, alt:f.name, className:"chat-preview-img"})
      : el("iframe", {src:url, className:"chat-preview-frame", title:f.name})));
}

/* ---------- OnlyOffice ---------- */
async function chatOpenOffice(f){
  chatCloseOffice();
  let cfg;
  try {
    const r = await fetch(`/api/office/config?id=${encodeURIComponent(CHAT.active)}&path=${encodeURIComponent(f.relpath)}`, {credentials:"same-origin"});
    const j = await r.json();
    if(r.status === 404){ toast("OnlyOffice is not enabled (Settings → OnlyOffice)", "info"); return; }
    if(!j.ok){ flash(j.error || "cannot open the editor"); return; }
    cfg = j.config;
    CHAT.office = {config:cfg, public_url:j.public_url, path:f.relpath, title:f.name};
  } catch(e){ flash(e.message); return; }
  chatSetArtifacts(true);
  $("chat-artifacts").classList.add("previewing");
  $("chat-office-title").textContent = f.name;
  $("chat-office").classList.remove("init-hidden");
  // Bring the agent in as a co-editor of the same session: its edits then show
  // up live here, and it receives the document's chat and comments. Best effort.
  op("office_collab_open", {id:CHAT.active, path:f.relpath}).catch(() => {});
  const mount = el("div", {id:"chat-office-mount"});
  $("chat-office-mount").replaceWith(mount);
  const base = (CHAT.office.public_url || "").replace(/\/$/, "");
  chatLoadScript(base + "/web-apps/apps/api/documents/api.js", () => {
    if(!window.DocsAPI){ flash("OnlyOffice SDK failed to load"); return; }
    try { CHAT.office.editor = new window.DocsAPI.DocEditor("chat-office-mount", cfg); }
    catch(e){ flash("Editor error: " + e.message); }
  });
}
function chatLoadScript(src, onload){
  const s = document.createElement("script");
  s.src = src; s.async = true; s.onload = onload;
  s.onerror = () => flash("Could not load OnlyOffice from " + src);
  document.head.append(s);
}
function chatCloseOffice(){
  if(CHAT.office && CHAT.office.editor){
    try { CHAT.office.editor.destroyEditor(); } catch(e){}
  }
  CHAT.office = null;
  const o = $("chat-office"); if(o) o.classList.add("init-hidden");
  $("chat-artifacts").classList.remove("previewing");
}

/* ---------- polling / state ---------- */
function chatStartPoll(){
  chatStopPoll();
  CHAT.poll = setInterval(() => {
    if(document.hidden || !CHAT.open) return;
    if(CHAT.active) chatLoadHistory(false);
    CHAT.lastArtifacts = (CHAT.lastArtifacts + 1) % 3;
    if(CHAT.active && CHAT.lastArtifacts === 0) chatLoadArtifacts();
    CREW.tick = (CREW.tick + 1) % 3;
    if(CHAT.active && CREW.tick === 0) chatLoadActivity(false);
    CHAT.osTick = ((CHAT.osTick || 0) + 1) % 10;
    if(CHAT.osTick === 0) chatLoadOffice();
  }, 1600);
}
function chatStopPoll(){ if(CHAT.poll){ clearInterval(CHAT.poll); CHAT.poll = null; } }

/* Called by transport.js whenever a state frame arrives. */
function chatOnState(){
  if(!CHAT.open) return;
  chatLoadList();
  if(CHAT.active && CHAT.info){ chatRenderHead(); chatRenderAsks(); }
}

/* ---------- artifact panel: show/hide, drag-resize and parallel ---------- */
function chatSetArtifacts(show){
  $("chat-artifacts").classList.toggle("hidden", !show);
  $("chat-split").classList.toggle("hidden", !show);
}
function chatArtifactsShown(){ return !$("chat-artifacts").classList.contains("hidden"); }
function chatArtWidth(){ return parseInt(store.get("at.chat.artw") || "370", 10) || 370; }
function chatSetArtWidth(px){
  const chat = $("chat");
  const max = Math.max(260, chat.clientWidth - 260);
  const w = Math.max(240, Math.min(max, Math.round(px)));
  chat.style.setProperty("--art-w", w + "px");
  store.set("at.chat.artw", String(w));
}
/* Parallel: half the window for the chat, half for the artifact (OnlyOffice). */
function chatParallel(){
  const chat = $("chat");
  chatSetArtifacts(true);
  chatSetArtWidth(Math.max(240, (chat.clientWidth - 290) / 2));
}
(function initArtResize(){
  const split = $("chat-split"); if(!split) return;
  $("chat").style.setProperty("--art-w", chatArtWidth() + "px");
  let dragging = false;
  split.addEventListener("pointerdown", (e) => {
    dragging = true; document.body.classList.add("col-resizing");
    try { split.setPointerCapture(e.pointerId); } catch(_){}
  });
  split.addEventListener("pointermove", (e) => {
    if(!dragging) return;
    chatSetArtWidth($("chat").getBoundingClientRect().right - e.clientX);
  });
  const stop = (e) => {
    if(!dragging) return; dragging = false; document.body.classList.remove("col-resizing");
    try { split.releasePointerCapture(e.pointerId); } catch(_){}
  };
  split.addEventListener("pointerup", stop);
  split.addEventListener("pointercancel", stop);
})();

/* ---------- OnlyOffice status button (artifacts head) ----------
   Shows at a glance whether Docker / the Document Server are up, and lets the
   service be started, restarted or stopped without leaving the chat. */
const OS = {status:null, busy:false};
async function chatLoadOffice(){
  try { const r = await op("office_status"); OS.status = r.status || null; }
  catch(e){ OS.status = null; }
  chatRenderOffice();
}
function chatRenderOffice(){
  const b = $("chatOffice"); if(!b) return;
  const s = OS.status;
  let state = "unknown";
  if(s) state = !s.docker ? "nodocker" : s.healthy ? "ok" : (s.running ? "starting" : "off");
  b.dataset.state = state;
  const label = {ok:"OnlyOffice", starting:"OnlyOffice…", off:"OnlyOffice off",
                 nodocker:"No Docker", unknown:"OnlyOffice ?"}[state];
  const title = {ok:"OnlyOffice is healthy — click for actions",
                 starting:"OnlyOffice is starting… click for actions",
                 off:"OnlyOffice is not running — click to start it",
                 nodocker:"Docker is not reachable — click for options",
                 unknown:"OnlyOffice status unknown — click to re-check"}[state];
  b.querySelector(".os-lbl").textContent = label;
  b.title = title;
}
function chatOfficeMenu(anchor){
  const s = OS.status || {};
  const items = [];
  if(s.docker === false){
    items.push({label:"How to install Docker…", icon:"alert", run:()=>toast("Install Docker, add your user to the 'docker' group, then re-login.", "info", 7000)});
  } else if(s.healthy){
    items.push({label:"Restart OnlyOffice", icon:"refresh", run:()=>chatOfficeAction("restart")});
  } else {
    items.push({label: s.running ? "Restart OnlyOffice" : "Start OnlyOffice", icon:"refresh",
      run:()=>chatOfficeAction(s.running ? "restart" : "start")});
  }
  if(s.running) items.push({label:"Stop OnlyOffice", icon:"stop", danger:true, run:()=>chatOfficeAction("stop")});
  items.push("-");
  items.push({label:"Refresh status", icon:"refresh", run:chatLoadOffice});
  items.push({label:"Copy diagnostics", icon:"copy",
    run:()=>copyText(JSON.stringify(OS.status || {}, null, 2), "OnlyOffice status copied")});
  openMenu(anchor, items);
}
async function chatOfficeAction(action){
  if(OS.busy) return;
  OS.busy = true;
  toast(action === "stop" ? "Stopping OnlyOffice…"
        : "Starting OnlyOffice… (the first run pulls the image and can take a while)", "info", 6000);
  try { await op("office_start", {action}); }
  catch(e){ flash(e.message); }
  OS.busy = false;
  [1500, 4000, 8000, 15000, 25000].forEach(ms => setTimeout(chatLoadOffice, ms));
}

/* ---------- wiring ---------- */
$("chatExit").onclick = () => setInterface("cowork");
$("chatNew").onclick = chatNew;
$("chatSend").onclick = chatSend;
$("chatTools").onclick = (e) => { e.stopPropagation(); openMenu($("chatTools"), [
  {label:"Attach a file…", icon:"paperclip", run:() => $("chat-file").click()},
  ...(CHAT.info && CHAT.info.agent_id ? [{label:"Send a key… (Esc, Ctrl+C…)", icon:"terminal",
    run:() => openKeysMenu($("chatTools"), CHAT.info.agent_id)}] : []),
  {label: chatArtifactsShown() ? "Hide artifacts" : "Show artifacts", icon:"file",
   run:() => chatSetArtifacts(!chatArtifactsShown())},
  {label:"Parallel view (50/50)", icon:"columns", run:chatParallel},
]); };
$("chat-file").onchange = (e) => { chatAddFiles([...e.target.files]); e.target.value = ""; };
$("chatArtWide").onclick = chatParallel;
$("chatArtRefresh").onclick = chatLoadArtifacts;
$("chatArtClose").onclick = () => chatSetArtifacts(false);
$("chatOffice").onclick = (e) => { e.stopPropagation(); chatOfficeMenu($("chatOffice")); };
$("chat-office-close").onclick = chatCloseOffice;
$("chat-q").oninput = (e) => { CHAT.q = e.target.value; chatRenderList(); };
$("chat-input").addEventListener("input", chatGrow);
$("chat-input").addEventListener("keydown", (e) => {
  if(e.isComposing) return;
  if(e.key === "Enter" && !e.shiftKey){ e.preventDefault(); chatSend(); }
});
$("chat-feed").addEventListener("contextmenu", (e) => {
  if(!CHAT.info || !CHAT.info.agent_id) return;
  e.preventDefault();
  openAgentMenu(e, CHAT.info.agent_id);
});
document.addEventListener("keydown", (e) => {
  if(e.key === "Escape" && CHAT.open && CHAT.office){ e.stopPropagation(); chatCloseOffice(); }
});

/* ---------- interface init: apply the remembered top-level mode ---------- */
renderModeSwitch();
$("modeSwitch").querySelectorAll("button").forEach(b => {
  b.onclick = () => setInterface(b.dataset.mode);
});
if(currentInterface() === "chat") chatOn();
