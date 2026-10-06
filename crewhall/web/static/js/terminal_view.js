"use strict";
/* Reusable xterm.js terminal view for the main pane. Loads xterm lazily and
   speaks the binary protocol over /ws/terminal/<id>. */
let __xtermReady = null;
function loadXterm(){
  if(window.Terminal && window.FitAddon && window.SearchAddon) return Promise.resolve();
  if(__xtermReady) return __xtermReady;
  const add = (src) => new Promise((res, rej) => {
    const s = document.createElement("script"); s.src = src; s.onload = res; s.onerror = rej;
    document.head.append(s);
  });
  __xtermReady = add("/static/vendor/xterm/xterm.js")
    .then(() => add("/static/vendor/xterm/addon-fit.js"))
    .then(() => add("/static/vendor/xterm/addon-search.js"));
  return __xtermReady;
}

async function terminalTicket(id, mode){
  const r = await fetch("/api/terminal-ticket", {method:"POST", credentials:"same-origin",
    headers:{"Content-Type":"application/json"}, body: JSON.stringify({id, mode})});
  return {status:r.status, body: await r.json().catch(()=>({}))};
}

function mountTerminalView(container, termId, opts){
  opts = opts || {};
  const st = {mode:"read", readonly:!!opts.readonly, closed:false, ws:null, retry:0,
              timer:null, ping:null, disposed:false, cols:0, rows:0};
  let term=null, fit=null, search=null;
  const u16 = n => { const b = new Uint8Array(2); new DataView(b.buffer).setUint16(0, n); return b; };
  const connEl = () => document.getElementById("termx-conn");
  const hostEl = () => document.getElementById("termx-host");

  function setConn(text, cls){
    const c = connEl(); if(c){ c.textContent = text; c.className = "chip " + (cls||""); }
    if(opts.onState) opts.onState({mode:st.mode, readonly:st.readonly, closed:st.closed, label:text, kind:cls});
  }
  function send(type, body){
    if(!st.ws || st.ws.readyState !== WebSocket.OPEN) return;
    const payload = new Uint8Array(1 + (body ? body.length : 0));
    payload[0] = type; if(body) payload.set(body, 1);
    st.ws.send(payload);
  }
  function applyControl(c){
    if(typeof c.readonly === "boolean") st.readonly = c.readonly;
    if(typeof c.mode === "string") st.mode = c.mode;
    if(c.host !== undefined){ const h = hostEl(); if(h) h.textContent = c.host || "local"; }
    // Never resize from the control frame: this client fits its own container
    // (and, when it holds the keyboard, tells the server its size). Resizing to
    // the server's cols/rows would leave empty space below the grid.
    if(c.closed){ st.closed = true; setConn("closed", "s-error"); }
    else if(st.readonly) setConn("read-only", "s-waiting_input");
    else if(st.mode === "write") setConn("connected · keyboard", "s-ready");
    else setConn("connected · view", "s-ready");
    const claim = document.getElementById("termx-claim");
    if(claim) claim.disabled = st.readonly;
  }
  function connect(){
    terminalTicket(termId, st.mode === "write" ? "write" : (opts.wantMode || "write"))
      .then(async (res) => {
        if(st.disposed) return;
        if(res.status === 403 && window.Terminals && window.Terminals.unlock){
          const ok = await window.Terminals.unlock();
          if(ok) res = await terminalTicket(termId, opts.wantMode || "write");
        }
        if(res.status !== 200 || !res.body.ticket){ setConn("locked", "s-error"); return; }
        openSocket(res.body.ticket);
      })
      .catch(() => setConn("error", "s-error"));
  }
  function openSocket(ticket){
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = proto + "//" + location.host + "/ws/terminal/" + encodeURIComponent(termId)
      + "?ticket=" + encodeURIComponent(ticket);
    setConn(st.retry ? "reconnecting…" : "connecting…", st.retry ? "s-working" : "");
    try{ st.ws = new WebSocket(url); }catch(e){ schedule(); return; }
    st.ws.binaryType = "arraybuffer";
    st.ws.onopen = () => {
      st.retry = 0;
      try{ fit.fit(); }catch(e){}
      send(0x03, new Uint8Array([...u16(term.cols), ...u16(term.rows)]));
      send(0x04, null);
      if(st.ping) clearInterval(st.ping);
      st.ping = setInterval(() => send(0x04, null), 20000);
      if(opts.onOpen) opts.onOpen();
    };
    st.ws.onmessage = (ev) => {
      const buf = new Uint8Array(ev.data); if(buf.length < 1) return;
      const type = buf[0], body = buf.subarray(1);
      if(type === 0x02){ if(term) term.write(body); }
      else if(type === 0x06){ try{ applyControl(JSON.parse(new TextDecoder().decode(body))); }catch(e){} }
    };
    st.ws.onclose = () => {
      if(st.ping){ clearInterval(st.ping); st.ping = null; }
      if(!st.closed && !st.disposed) schedule();
    };
    st.ws.onerror = () => { try{ st.ws.close(); }catch(e){} };
  }
  function schedule(){
    if(st.closed || st.disposed) return;
    const base = Math.min(10000, 500 * Math.pow(2, st.retry));
    const jitter = base * 0.2 * Math.random();
    st.retry += 1;
    setConn("reconnecting…", "s-working");
    st.timer = setTimeout(connect, base + jitter);
  }

  let ro = null;
  const api = {
    send,
    claim(){ send(0x07, null); },
    focus(){ if(term) term.focus(); },
    fit(){ if(fit){ try{ fit.fit(); }catch(e){} } },
    search(q, next){ if(search && q){ next ? search.findNext(q) : search.findPrevious(q); } },
    dispose(){
      st.disposed = true; st.closed = true;
      if(st.timer) clearTimeout(st.timer);
      if(st.ping) clearInterval(st.ping);
      if(ro){ try{ ro.disconnect(); }catch(e){} }
      if(st.ws){ try{ st.ws.close(); }catch(e){} }
      if(term){ try{ term.dispose(); }catch(e){} }
      term = null;
    },
    state: st,
  };

  loadXterm().then(() => {
    if(st.disposed) return;
    container.replaceChildren();
    term = new Terminal({convertEol:false, cursorBlink:true, scrollback:5000,
      fontFamily:"ui-monospace, SFMono-Regular, Menlo, monospace", fontSize:13,
      theme:{background:"#0b0d12", foreground:"#e6e8ee"}});
    fit = new FitAddon.FitAddon(); search = new SearchAddon.SearchAddon();
    term.loadAddon(fit); term.loadAddon(search);
    term.open(container);
    try{ fit.fit(); }catch(e){}
    // Refit whenever the container's size changes (e.g. the pane becomes
    // visible or the window is resized) so the terminal fills the panel.
    if(window.ResizeObserver){
      ro = new ResizeObserver(() => { if(!st.disposed) api.fit(); });
      ro.observe(container);
    }
    requestAnimationFrame(() => { if(!st.disposed) api.fit(); });
    term.onData(d => { if(st.mode === "write" && !st.readonly && !st.closed) send(0x01, new TextEncoder().encode(d)); });
    term.onResize(({cols, rows}) => { if(st.mode === "write" && !st.readonly) send(0x03, new Uint8Array([...u16(cols), ...u16(rows)])); });
    const searchInput = document.getElementById("termx-search");
    if(searchInput){
      searchInput.onkeydown = (e) => { if(e.key === "Enter"){ e.preventDefault(); api.search(searchInput.value, !e.shiftKey); } };
    }
    connect();
    if(opts.focus) term.focus();
  }).catch(() => setConn("xterm failed to load", "s-error"));

  return api;
}
