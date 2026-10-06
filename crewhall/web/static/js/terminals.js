"use strict";
/* Terminals tab (Phase 1: plain-text view, no xterm yet).
   Closed by default: the button only appears when the daemon reports
   terminals_enabled, and every op is also gated server-side. */
(function(){
  let dlg = null, listEl = null, viewEl = null, inputEl = null, titleEl = null;
  let statusEl = null, unlockBtn = null;
  let current = null, poll = null, hosts = [];

  function keyRow(){
    const keys = [["Ctrl-C","CTRL_C"],["Esc","ESC"],["Tab","TAB"],["Enter","ENTER"],
                  ["↑","UP"],["↓","DOWN"],["←","LEFT"],["→","RIGHT"]];
    return el("div", {className:"keys"}, keys.map(([label,key]) =>
      el("button", {className:"btn sm", type:"button", onclick:() => sendKey(key)}, label)));
  }

  function sendKey(key){
    if(!current) return;
    op("terminal_key", {id: current.session_id, key}).catch(e => toast(String(e), "error"));
  }

  async function requestTicket(id, mode){
    const r = await fetch("/api/terminal-ticket", {
      method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({id, mode}),
    });
    return {status: r.status, body: await r.json().catch(() => ({}))};
  }

  function setLocked(v){
    if(statusEl) statusEl.textContent = v ? "locked" : "unlocked";
    if(unlockBtn) unlockBtn.classList.toggle("init-hidden", !v);
  }

  async function hasAnyToken(){
    try{ const r = await op("terminal_token_list"); return (r.tokens || []).length > 0; }
    catch(e){ return true; }  // can't tell: let the user paste one
  }

  async function issueAndUnlock(){
    try{
      const r = await op("terminal_token_issue",
        {scope:"write", hosts:null, ttl:8*3600, label:"web"});
      if(typeof showIssuedToken === "function") showIssuedToken(r.token, r.record);
      const u = await fetch("/api/terminal-unlock", {method:"POST", credentials:"same-origin",
        headers:{"Content-Type":"application/json"}, body: JSON.stringify({token:r.token})});
      if(u.ok){ setLocked(false); toast("Terminals unlocked", "ok"); return true; }
      toast("Created the token; paste it with Unlock", "info");
      return false;
    }catch(e){ toast(String(e), "error"); return false; }
  }

  async function unlockTerminal(){
    if(!await hasAnyToken()){
      if(!await confirmDlg({title:"No terminal tokens yet",
        message:"Create a write-scoped terminal token now? It will be shown once, then used to unlock this browser.",
        ok:"Create token"})) return false;
      return issueAndUnlock();
    }
    const token = await promptDlg({title:"Unlock terminals", ok:"Unlock",
      sub:"Paste a terminal token with scope write (Settings → Access & network → Terminal tokens).",
      fields:[{key:"token", label:"Terminal token", mono:true, required:true}]});
    if(!token) return false;
    const r = await fetch("/api/terminal-unlock", {
      method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({token: token.token}),
    });
    if(!r.ok){ toast("Invalid or read-only terminal token", "error"); return false; }
    setLocked(false); toast("Terminals unlocked", "ok"); return true;
  }

  async function openTerminalPage(id, mode){
    const want = mode || (current && current.readonly ? "read" : "write");
    let res = await requestTicket(id, want).catch(() => ({status: 0, body: {}}));
    if(res.status === 403){
      if(!await unlockTerminal()) return;
      res = await requestTicket(id, want).catch(() => ({status: 0, body: {}}));
    }
    let url = "/terminal.html?id=" + encodeURIComponent(id);
    if(res.body && res.body.ticket) url += "&ticket=" + encodeURIComponent(res.body.ticket);
    window.open(url, "_blank", "noopener");
  }

  function sendLine(){
    if(!current || !inputEl) return;
    const text = inputEl.value;
    if(!text) return;
    inputEl.value = "";
    op("terminal_write", {id: current.session_id, text, enter: true})
      .catch(e => toast(String(e), "error"));
  }

  async function refreshList(){
    try{
      const r = await op("terminal_list", {});
      setLocked(false);
      renderList(r.terminals || []);
    }catch(e){
      if(String(e).includes("forbidden")){
        setLocked(true);
        if(listEl) setKids(listEl, el("div", {className:"term-locked"},
          el("p", {className:"muted"}, "Terminals are locked for this session."),
          el("button", {className:"btn primary", type:"button", onclick: async () => {
            if(await unlockTerminal()) refreshList();
          }}, "Unlock terminals")));
      }
    }
  }

  function renderList(terms){
    if(!listEl) return;
    setKids(listEl, terms.length ? terms.map(t => {
      const row = el("button", {className:"btn term-row", type:"button",
        onclick: () => select(t)},
        el("span", {className:"nm"}, t.title || t.session_id),
        el("span", {className:"muted"}, t.host || "local"),
        el("span", {className:"muted"}, t.status),
        t.readonly ? el("span", {className:"chip"}, "read-only") : null);
      row.dataset.term = t.session_id;
      return row;
    }) : el("p", {className:"muted"}, "No terminals yet."));
    if(current){
      const again = terms.find(t => t.session_id === current.session_id);
      if(again) current = again; else { current = null; if(viewEl) viewEl.textContent = ""; }
    }
    if(titleEl) titleEl.textContent = current ? (current.title || current.session_id) : "Select a terminal";
  }

  function select(t){
    current = t;
    if(inputEl){ inputEl.disabled = !!t.readonly; inputEl.placeholder =
      t.readonly ? "read-only terminal" : "Type and press Enter…"; }
    if(titleEl) titleEl.textContent = t.title || t.session_id;
    startPolling();
    refreshList();
  }

  function startPolling(){
    stopPolling();
    poll = setInterval(async () => {
      if(!dlg || !dlg.open || !current) return;
      try{
        const r = await op("terminal_capture", {id: current.session_id, recent: true, max_lines: 500});
        if(viewEl) viewEl.textContent = r.output || "";
        if(viewEl) viewEl.scrollTop = viewEl.scrollHeight;
      }catch(e){ /* keep polling */ }
    }, 1000);
  }
  function stopPolling(){ if(poll){ clearInterval(poll); poll = null; } }

  function newForm(){
    const hostSel = el("select", {className:"select", id:"term-host"},
      el("option", {value:""}, "This machine"),
      ...hosts.map(h => el("option", {value:h.name}, h.name)));
    const cwd = el("input", {className:"input mono", id:"term-cwd", placeholder:"/absolute/path (optional)"});
    const title = el("input", {className:"input", id:"term-title", placeholder:"Title (optional)"});
    const shell = el("select", {className:"select", id:"term-shell"},
      el("option", {value:""}, "Default shell"),
      el("option", {value:"bash"}, "bash"), el("option", {value:"zsh"}, "zsh"),
      el("option", {value:"sh"}, "sh"), el("option", {value:"fish"}, "fish"));
    const ro = el("input", {type:"checkbox", id:"term-ro"});
    const shellField = el("div", {className:"field"}, el("label", {}, "Shell (local only)"), shell);
    hostSel.addEventListener("change", () => {
      shellField.style.display = hostSel.value ? "none" : "";
    });
    const form = el("div", {className:"stack term-new"},
      el("div", {className:"field"}, el("label", {}, "Run on"), hostSel),
      el("div", {className:"field"}, el("label", {}, "Working directory"), cwd),
      el("div", {className:"field"}, el("label", {}, "Title"), title),
      shellField,
      el("label", {className:"field"}, ro, " Read-only"),
      el("button", {className:"btn primary", type:"button", onclick: async () => {
        const payload = {host: hostSel.value || null, cwd: cwd.value || null,
                         title: title.value || null, readonly: ro.checked};
        if(!hostSel.value && shell.value) payload.shell = shell.value;
        const create = async () => {
          const r = await op("terminal_create", payload);
          toast("Terminal created", "ok", 2000);
          await refreshList();
          select(r.terminal);
        };
        try{
          await create();
        }catch(e){
          if(String(e).includes("forbidden")){
            if(await unlockTerminal()){
              try{ await create(); }catch(e2){ toast(String(e2), "error"); }
            } else {
              toast("Terminals need a write-scoped token (Unlock)", "error");
            }
          } else {
            toast(String(e), "error");
          }
        }
      }}, "Create terminal"));
    return form;
  }

  async function ensureDialog(){
    if(dlg) return dlg;
    dlg = el("dialog", {id:"termDlg", className:"settings", "aria-label":"Terminals"});
    const closeBtn = el("button", {className:"btn icon ghost", "aria-label":"Close", onclick: () => dlg.close()}, ic("x"));
    const newBtn = el("button", {className:"btn", type:"button", onclick: () => {
      if(!dlg.open) dlg.showModal();
      const box = $("term-new-slot");
      setKids(box, newForm());
    }}, "New terminal");
    titleEl = el("span", {className:"muted"}, "Select a terminal");
    viewEl = el("pre", {className:"term", id:"term-view"});
    listEl = el("div", {className:"term-list", id:"term-list"});
    inputEl = el("input", {className:"input mono", id:"term-input", autocomplete:"off",
      onkeydown: (ev) => { if(ev.key === "Enter"){ ev.preventDefault(); sendLine(); } }});
    statusEl = el("span", {className:"muted", id:"term-lock-state"}, "");
    unlockBtn = el("button", {className:"btn sm init-hidden", type:"button", id:"term-unlock",
      onclick: async () => { if(await unlockTerminal()) refreshList(); }}, "Unlock");
    const top = el("div", {className:"inbox-top"},
      el("h3", {}, "Terminals"), statusEl, unlockBtn,
      el("span", {className:"spacer"}), titleEl, newBtn, closeBtn);
    const body = el("div", {className:"term-body"},
      listEl,
      el("div", {className:"term-main"},
        viewEl,
        el("div", {className:"term-line"}, inputEl,
          el("button", {className:"btn primary sm", type:"button", onclick: sendLine}, "Send"),
          el("button", {className:"btn sm", type:"button", title:"Full terminal (xterm) in a new tab",
            onclick: () => { if(current) openTerminalPage(current.session_id); }}, "Open")),
        keyRow(),
        el("button", {className:"btn danger sm", type:"button", onclick: async () => {
          if(!current) return;
          try{ await op("terminal_close", {id: current.session_id}); current = null;
               if(viewEl) viewEl.textContent = ""; await refreshList(); }
          catch(e){ toast(String(e), "error"); }
        }}, "Close terminal")),
      el("div", {id:"term-new-slot"}));
    setKids(dlg, top, body);
    document.body.append(dlg);
    dlg.addEventListener("close", stopPolling);
    return dlg;
  }

  async function open(){
    try{
      const meta = await op("meta_info", {});
      if(!meta.terminals_enabled){ toast("Terminals are disabled", "error"); return; }
      hosts = meta.hosts || [];
    }catch(e){ hosts = []; }
    await ensureDialog();
    if(!dlg.open) dlg.showModal();
    await refreshList();
  }

  function installButton(){
    const topbar = document.querySelector(".topbar");
    if(!topbar || document.getElementById("termBtn")) return;
    const btn = el("button", {className:"btn icon ghost init-hidden", id:"termBtn",
      "aria-label":"Terminals", title:"Terminals — raw interactive shells",
      onclick: open}, ic("terminal"));
    const settingsBtn = document.getElementById("settingsBtn");
    topbar.insertBefore(btn, settingsBtn || null);
    return btn;
  }

  async function boot(){
    const btn = installButton();
    if(!btn) return;
    try{
      const meta = await op("meta_info", {});
      if(meta.terminals_enabled) btn.classList.remove("init-hidden");
    }catch(e){}
  }

  window.Terminals = {open, unlock: unlockTerminal, openPage: openTerminalPage, refresh: refreshList};
  if(document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
