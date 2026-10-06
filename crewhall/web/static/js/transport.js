"use strict";
/* ---------- transport ---------- */
async function op(o, params = {}) {
  const r = await fetch("/api/op", { method:"POST",
    headers:{"Content-Type":"application/json"},
    credentials:"same-origin",
    body: JSON.stringify({op:o, ...params}) });
  if (r.status === 401) { location.href = "/login"; throw new Error("authentication required"); }
  const j = await r.json().catch(()=>({ok:false,error:r.statusText}));
  if (!j.ok) throw new Error(j.error || "unknown error");
  return j;
}

function setConn(ok){ const c = $("conn"); c.textContent = ok ? "connected" : "offline — retrying…"; c.className = "conn " + (ok ? "ok" : "bad"); }
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const w = new WebSocket(`${proto}://${location.host}/ws`);
  S.ws = w;
  w.onerror = () => { /* e.g. 401 on handshake -> go to login */
    fetch("/api/op", {method:"POST",credentials:"same-origin",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({op:"ping"})})
      .then(r => { if (r.status === 401) location.href = "/login"; }); };
  w.onopen = () => { setConn(true); sendFocus(S.selected); };
  w.onclose = () => { setConn(false); setTimeout(connect, 1200); };
  w.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "state") {
      // crewhall was updated: this page's code is stale, load the new one.
      if (m.version && S.version && m.version !== S.version) { location.reload(); return; }
      if (m.version) { S.version = m.version; $("version").textContent = "v" + m.version; }
      S.state = { agents:m.agents, teams:m.teams, messages:m.messages||[], hosts:m.hosts||[] };
      trackStates(S.state.agents);
      // Auto-select the first agent on first load so the transcript is visible.
      if (!S.selected && S.state.agents.length && !S.autoSelected) { S.autoSelected = true; select(S.state.agents[0].agent_id); }
      render();
      if (S.selected) renderTerm();
    }
    if (m.type === "output") { S.output[m.agent_id] = m.output;
      if (S.selected === m.agent_id) renderTerm(); }
  };
}
function sendFocus(id){
  try { if(id && S.ws && S.ws.readyState===1) S.ws.send(JSON.stringify({type:"focus", agent_id:id})); }
  catch(e){}
}

/* ---------- activity tracking: when did an agent start working / finish ---------- */
function trackStates(agents){
  const seen = new Set();
  if(!S.intSig) S.intSig = {};
  if(typeof tlRecord === "function") tlRecord(agents, Date.now() / 1000);
  agents.forEach(a => {
    seen.add(a.agent_id);
    const was = S.prev[a.agent_id];
    if(was !== a.state){
      S.since[a.agent_id] = Date.now();
      if(was === "working"){
        S.doneAt[a.agent_id] = Date.now();
        const nm = a.name || a.agent_id;
        if(a.state === "error"){ toast(`${nm} stopped with an error`, "err"); notify(`${nm} errored`, "The agent stopped with an error.", {tag:"state:" + a.agent_id}); }
        else if(a.state === "exited"){ toast(`${nm} exited`, "info"); notify(`${nm} exited`, "The agent's process ended.", {tag:"state:" + a.agent_id}); }
        else if(a.agent_id !== S.selected || document.hidden){ toast(`${nm} finished its turn`, "ok"); notify(`${nm} finished`, "Its turn is done.", {tag:"turn:" + a.agent_id}); }
      }
      if(was !== undefined && a.state === "waiting_input" && was !== "waiting_input" && was !== "working" && a.agent_id !== S.selected)
        toast(`${a.name || a.agent_id} is waiting for you`, "work");
    }
    S.prev[a.agent_id] = a.state;
    // New permission/question raised by an agent since the previous snapshot.
    const ids = (a.interactions || []).map(i => i.id).sort().join(",");
    const prevIds = S.intSig[a.agent_id];
    if(prevIds !== undefined && ids && ids !== prevIds){
      const known = new Set(prevIds ? prevIds.split(",") : []);
      (a.interactions || []).forEach(it => { if(!known.has(it.id)) notify(`${a.name || a.agent_id} asks`, it.title || "Needs your answer", {tag:it.id}); });
    }
    S.intSig[a.agent_id] = ids;
  });
  Object.keys(S.prev).forEach(id => { if(!seen.has(id)){ delete S.prev[id]; delete S.since[id]; delete S.doneAt[id]; delete S.intSig[id]; } });
}
function tick(){
  document.querySelectorAll("[data-since]").forEach(n => {
    n.textContent = fmtClock((Date.now() - (S.since[n.dataset.since]||Date.now()))/1000); });
}
setInterval(tick, 1000);

function updateChrome(){
  const agents = S.state?.agents || [];
  const working = agents.filter(a => a.state === "working").length;
  const attn = agents.filter(needsYou).length;
  const pending = agents.reduce((n, a) => n + (a.interactions || []).length, 0);
  document.title = (pending ? `(${pending}) ` : attn ? `(${attn}) ` : "") + (working ? `● ${working} working · ` : "") + "crewhall";
  const color = working ? "#e3a008" : attn ? "#5b9dff" : "#3fb950";
  let link = document.querySelector("link[rel=icon]");
  if(!link){ link = el("link", {rel:"icon"}); document.head.append(link); }
  const href = "data:image/svg+xml," + encodeURIComponent(`<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='8' fill='#12151c'/><circle cx='16' cy='16' r='7' fill='${color}'/></svg>`);
  if(link.getAttribute("href") !== href) link.setAttribute("href", href);
  const s = $("summary");
  setKids(s,
    el("span", {className:"chip"}, `${agents.length} agent${agents.length===1?"":"s"}`),
    el("span", {className:"chip"}, `${(S.state?.teams||[]).length} team${(S.state?.teams||[]).length===1?"":"s"}`),
    working ? el("span", {className:"pill s-working", title:"Agents working right now"}, dot("working"), `${working} working`) : null,
    attn ? el("span", {className:"pill s-waiting_input", title:"Agents waiting for you"}, dot("waiting_input"), `${attn} need${attn===1?"s":""} you`) : null);
}

