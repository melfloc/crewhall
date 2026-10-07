"use strict";
/* xterm.js client for one terminal. Talks the binary protocol over
   /ws/terminal/<id> and reconnects with exponential backoff. */
(function(){
  const params = new URLSearchParams(location.search);
  const id = params.get("id") || "";
  const ticket = params.get("ticket") || "";
  const badge = document.getElementById("term-host-badge");
  const conn = document.getElementById("term-conn-state");
  const claimBtn = document.getElementById("term-claim");
  const closeBtn = document.getElementById("term-close");
  const searchInput = document.getElementById("term-search");

  let mode = "read", readonly = false, closed = false, ws = null, retry = 0, timer = null;
  let pingTimer = null;

  const term = new Terminal({
    convertEol: false, cursorBlink: true, scrollback: 5000,
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: 13,
    theme: { background: "#0b0d12", foreground: "#e6e8ee" },
  });
  const fit = new FitAddon.FitAddon();
  const search = new SearchAddon.SearchAddon();
  term.loadAddon(fit);
  term.loadAddon(search);
  term.open(document.getElementById("xterm"));
  const ta = document.querySelector(".xterm-helper-textarea");
  if(ta){
    ta.setAttribute("autocapitalize","off"); ta.setAttribute("autocorrect","off");
    ta.setAttribute("autocomplete","off"); ta.setAttribute("spellcheck","false");
    ta.setAttribute("inputmode","text");
  }
  try { fit.fit(); } catch (e) {}

  function setConn(text, cls){
    conn.textContent = text;
    conn.className = "tconn " + (cls || "");
  }

  function send(type, body){
    if(!ws || ws.readyState !== WebSocket.OPEN) return;
    const payload = new Uint8Array(1 + (body ? body.length : 0));
    payload[0] = type;
    if(body) payload.set(body, 1);
    ws.send(payload);
  }
  const u16 = n => { const b = new Uint8Array(2); new DataView(b.buffer).setUint16(0, n); return b; };
  const enc = s => new TextEncoder().encode(s);

  function applyControl(c){
    if(typeof c.readonly === "boolean") readonly = c.readonly;
    if(typeof c.mode === "string") mode = c.mode;
    if(typeof c.host === "string" || c.host === null) badge.textContent = c.host || "local";
    // Fit to this window; do not adopt the server's cols/rows (it would leave
    // empty space below the grid when the sizes differ).
    if(c.closed){ closed = true; setConn("closed", "closed"); }
    claimBtn.disabled = readonly;
    if(c.closed) setConn("closed", "closed");
    else if(readonly) setConn("readonly", "readonly");
    else if(mode === "write") setConn("connected · keyboard", "connected");
    else setConn("connected · read-only view", "connected");
  }

  function connect(){
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    let url = proto + "//" + location.host + "/ws/terminal/" + encodeURIComponent(id);
    if(ticket) url += "?ticket=" + encodeURIComponent(ticket);
    setConn(retry ? "reconnecting…" : "connecting…", retry ? "reconnecting" : "");
    try { ws = new WebSocket(url); } catch (e) { scheduleReconnect(); return; }
    ws.binaryType = "arraybuffer";
    ws.onopen = () => {
      retry = 0;
      try { fit.fit(); } catch (e) {}
      send(0x03, new Uint8Array([...u16(term.cols), ...u16(term.rows)]));
      send(0x04, null);  // ping
      if(pingTimer) clearInterval(pingTimer);
      pingTimer = setInterval(() => send(0x04, null), 20000);
    };
    ws.onmessage = ev => {
      const buf = new Uint8Array(ev.data);
      if(buf.length < 1) return;
      const type = buf[0], body = buf.subarray(1);
      if(type === 0x02){ term.write(body); }
      else if(type === 0x06){
        try { applyControl(JSON.parse(new TextDecoder().decode(body))); } catch (e) {}
      } else if(type === 0x05){ /* pong */ }
    };
    ws.onclose = () => {
      if(pingTimer){ clearInterval(pingTimer); pingTimer = null; }
      if(!closed) scheduleReconnect();
    };
    ws.onerror = () => { try { ws.close(); } catch (e) {} };
  }

  function scheduleReconnect(){
    if(closed) return;
    const base = Math.min(10000, 500 * Math.pow(2, retry));
    const jitter = base * 0.2 * Math.random();
    retry += 1;
    setConn("reconnecting…", "reconnecting");
    timer = setTimeout(connect, base + jitter);
  }

  term.onData(d => { if(mode === "write" && !readonly && !closed) send(0x01, enc(d)); });
  term.onResize(({cols, rows}) => {
    if(mode === "write" && !readonly) send(0x03, new Uint8Array([...u16(cols), ...u16(rows)]));
  });
  window.addEventListener("resize", () => { try { fit.fit(); } catch (e) {} });

  claimBtn.onclick = () => { send(0x07, null); };
  closeBtn.onclick = () => { send(0x02, null); window.close(); };

  // Ctrl+F: find in scrollback.
  document.addEventListener("keydown", e => {
    if((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f"){
      e.preventDefault(); searchInput.focus(); searchInput.select();
    }
  });
  function findNext(){ if(searchInput.value) search.findNext(searchInput.value, {incremental:false}); }
  searchInput.addEventListener("keydown", e => { if(e.key === "Enter"){ e.preventDefault(); findNext(); } });
  document.getElementById("term-search-next").onclick = findNext;

  connect();
})();
