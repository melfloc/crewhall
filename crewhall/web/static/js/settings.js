"use strict";
const _wb64u = buf => btoa(String.fromCharCode(...new Uint8Array(buf)))
  .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const _wunb64u = s => {
  s = String(s).replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(s), u = new Uint8Array(bin.length);
  for(let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
  return u;
};
/* ---------- Settings: providers, agents, access & network, maintenance, interface, emergency ---------- */
const SET_TABS = [["providers", "Providers"], ["agents", "Agents"], ["hosts", "Remote hosts"], ["terminals", "Terminals"],
                  ["access", "Access & network"],
                  ["maintenance", "Maintenance"], ["interface", "Interface"], ["audit", "Audit"], ["emergency", "Emergency"]];
const SK = { tab:"providers", data:null };

function setField(item, onChange){
  // One input for one schema entry; returns {node, get}.
  const id = "sf-" + item.key.replace(/\./g, "-");
  const locked = !!item.env;
  let input, get;
  if(item.type === "bool"){
    input = el("input", {type:"checkbox", id, checked:!!item.value, disabled:locked, onchange:onChange});
    get = () => input.checked;
  } else if(item.type === "choice"){
    input = el("select", {className:"input", id, disabled:locked, onchange:onChange},
      ...item.choices.map(c => el("option", {value:c, textContent:c, selected:c === item.value})));
    get = () => input.value;
  } else if(item.type === "int"){
    input = el("input", {className:"input", type:"number", id, value:item.value, min:item.min, max:item.max, disabled:locked, oninput:onChange});
    get = () => input.value === "" ? null : Number(input.value);
  } else {  // list / paths: one entry per line
    input = el("textarea", {className:"input mono", id, rows:3, value:(item.value||[]).join("\n"), disabled:locked, spellcheck:false, oninput:onChange});
    get = () => input.value;
  }
  const hint = [item.help, item.env ? `Set by the environment variable ${item.env}; unset it to change this here.` : null,
                item.restart ? "Applies after the next daemon restart or when the related feature is restarted." : null].filter(Boolean).join(" ");
  const box = item.type === "bool"
    ? el("div", {className:"field check-row"}, el("label", {htmlFor:id, className:"check"}, input, el("span", {}, item.label)), el("div", {className:"hint"}, hint))
    : el("div", {className:"field"}, el("label", {htmlFor:id}, item.label), input, el("div", {className:"hint"}, hint));
  return {node:box, get, item, input};
}

// A group of schema items with its own Save / Restore-defaults.
function settingsGroup(group, title, exclude = []){
  const items = SK.data.items.filter(i => i.group === group && !exclude.includes(i.key));
  const save = el("button", {className:"btn primary", type:"button", disabled:true}, "Save");
  const fields = items.map(it => setField(it, () => { save.disabled = false; }));
  save.onclick = async () => {
    const changes = {};
    fields.forEach(f => { if(f.input.disabled) return;
      const listy = f.item.type === "list" || f.item.type === "paths";
      const v = f.get(); if(JSON.stringify(v) !== JSON.stringify(listy ? (f.item.value||[]).join("\n") : f.item.value) ) changes[f.item.key] = v; });
    if(!Object.keys(changes).length){ save.disabled = true; return; }
    const confirmKeys = new Set(["terminals.enabled", "terminals.master_grants",
                                 "terminals.totp_grants"]);
    const needsConfirm = Object.keys(changes).some(k =>
      confirmKeys.has(k) || k.endsWith(".command") || k.endsWith(".env"));
    if(needsConfirm){
      const ok = await confirmTyped({title:"Confirm privileged change?", word:"CONFIRM", ok:"Apply",
        message:"This change lets the daemon run arbitrary code as your user. Type CONFIRM to proceed."});
      if(!ok) return;
    }
    try {
      SK.data = await op("settings_set", {changes, ...(needsConfirm ? {confirm:"CONFIRM"} : {})});
      toast("Settings saved", "ok"); renderSettings();
    }
    catch(e){ flash(e.message); }
  };
  const restore = el("button", {className:"btn", type:"button", onclick:async()=>{
    if(!await confirmDlg({title:`Restore ${title} defaults?`, message:"Values you changed here go back to their defaults.", ok:"Restore"})) return;
    try { SK.data = await op("settings_reset", {prefix:group}); toast("Defaults restored", "ok"); } catch(e){ flash(e.message); }
    renderSettings(); }}, "Restore defaults");
  return el("div", {className:"set-group"}, ...fields.map(f => f.node), el("div", {className:"dlg-actions", style:"justify-content:flex-start"}, save, restore));
}

async function confirmTyped({title, message, word = "CONFIRM", ok = "Apply"}){
  const input = el("input", {className:"input mono", id:"typed-confirm", placeholder:`Type ${word}`, autocomplete:"off", spellcheck:false});
  const go = el("button", {className:"btn danger solid", type:"submit", disabled:true}, ok);
  input.oninput = () => { go.disabled = input.value.trim() !== word; };
  const r = await openDlg(close => el("form", {onsubmit:e=>{ e.preventDefault(); if(!go.disabled) close(true); }},
    el("h3", {}, title), el("p", {className:"sub"}, message),
    el("div", {className:"field"}, el("label", {htmlFor:"typed-confirm"}, `Type ${word} to confirm`), input),
    el("div", {className:"dlg-actions"}, el("button", {className:"btn", type:"button", onclick:()=>close(undefined)}, "Cancel"), go)));
  return r === true;
}

/* ----- providers ----- */
function providerCard(p, status){
  const id = k => `pv-${p.kind}-${k}`;
  const enabled = el("input", {type:"checkbox", id:id("enabled"), checked:p.enabled});
  const mcp = el("input", {type:"checkbox", id:id("mcp"), checked:!!p.mcp, disabled:!p.mcp_supported});
  const command = el("input", {className:"input mono", id:id("command"), value:p.command, placeholder:`built-in: ${p.kind}`, spellcheck:false, autocomplete:"off"});
  const model = el("input", {className:"input mono", id:id("model"), value:p.default_model, placeholder:"provider default", spellcheck:false, autocomplete:"off"});
  const args = el("input", {className:"input mono", id:id("args"), value:p.default_args, placeholder:"e.g. --verbose", spellcheck:false, autocomplete:"off"});
  const env = el("textarea", {className:"input mono", id:id("env"), rows:3, spellcheck:false, placeholder:"NAME=value, one per line",
    value:Object.entries(p.env||{}).map(([k, v]) => `${k}=${v}`).join("\n")});
  const stat = el("div", {className:"prov-status", id:id("status")});
  const paint = (s) => { stat.className = "prov-status " + (s && s.ok ? "ok" : "bad");
    stat.replaceChildren(s ? (s.ok ? ic("check", "sm") : ic("alert", "sm")) : "",
      s ? (s.ok ? ` ${s.version || "installed"} · ${s.path}` : ` ${s.error || "not found"}`) : ""); };
  if(status) paint(status);
  const test = el("button", {className:"btn sm", type:"button", onclick:async()=>{
    try { const r = await op("provider_check", {kind:p.kind, command:command.value.trim() || null}); paint(r.providers[0]); }
    catch(e){ flash(e.message); } }}, "Test");
  const save = el("button", {className:"btn primary", type:"button", onclick:async()=>{
    const changes = {[`providers.${p.kind}.enabled`]:enabled.checked, [`providers.${p.kind}.command`]:command.value,
      [`providers.${p.kind}.default_model`]:model.value, [`providers.${p.kind}.default_args`]:args.value,
      [`providers.${p.kind}.env`]:env.value, [`providers.${p.kind}.mcp`]:mcp.checked};
    const envText = Object.entries(p.env||{}).map(([k, v]) => `${k}=${v}`).join("\n");
    const sensitive = command.value.trim() !== (p.command||"") || env.value.trim() !== envText.trim();
    let confirm = false;
    if(sensitive){
      if(!await confirmTyped({title:`Change the ${p.kind} command or environment?`,
        message:"The command decides which binary runs and the environment can carry credentials. "
          + "The change is audited.", word:"CONFIRM", ok:"Save"})) return;
      confirm = true;
    }
    // masked secrets come back as-is: the server keeps the stored value
    try { SK.data = await op("settings_set", confirm ? {changes, confirm:"CONFIRM"} : {changes});
      S.meta = null; toast(`${p.kind} saved`, "ok"); renderSettings(); }
    catch(e){ flash(e.message); } }}, "Save");
  const reset = el("button", {className:"btn", type:"button", onclick:async()=>{
    if(!await confirmDlg({title:`Restore ${p.kind} defaults?`, message:"Command, model, arguments and variables are cleared; it is enabled again.", ok:"Restore"})) return;
    try { SK.data = await op("settings_reset", {prefix:`providers.${p.kind}`}); S.meta = null; toast("Defaults restored", "ok"); } catch(e){ flash(e.message); }
    renderSettings(); }}, "Restore defaults");
  return el("div", {className:"prov-card" + (p.enabled ? "" : " off"), dataset:{provider:p.kind}},
    el("div", {className:"prov-head"}, el("span", {className:"avatar", style:`--h:${hue(p.kind)}`}, p.kind.charAt(0).toUpperCase()),
      el("div", {style:"flex:1;min-width:0"}, el("div", {className:"nm"}, p.kind), stat),
      el("label", {className:"check", htmlFor:id("enabled")}, enabled, el("span", {}, "Enabled"))),
    el("div", {className:"onb-row"},
      el("div", {className:"field"}, el("label", {htmlFor:id("command")}, "Command / binary"), el("div", {className:"row"}, command, test),
        el("div", {className:"hint"}, "Path or command to launch instead of the one in PATH (another install, a wrapper script).")),
      el("div", {className:"field"}, el("label", {htmlFor:id("model")}, "Default model"), model,
        el("div", {className:"hint"}, "Passed as --model unless the agent sets its own."))),
    el("div", {className:"field"}, el("label", {htmlFor:id("args")}, "Default arguments"), args),
    el("div", {className:"field"}, el("label", {htmlFor:id("env")}, "Environment variables"), env,
      el("div", {className:"hint"}, "Added to every agent of this provider. Values that look like secrets are hidden after saving; leave them as shown to keep them.")),
    el("div", {className:"field check-row"}, el("label", {className:"check", htmlFor:id("mcp")}, mcp,
      el("span", {}, "MCP server")),
      el("div", {className:"hint"}, p.mcp_supported
        ? "Exposes to this agent (off by default): whoami, its teammates, sending messages and starting a new session of its own. The daemon enforces identity and Team limits."
        : "This CLI has no verified way to inject an MCP server; leave off.")),
    (p.risks && p.risks.length) ? el("div", {className:"prov-risks"}, ...p.risks.map(r =>
      el("div", {className:"risk"}, ic("alert", "sm"), " ", r))) : null,
    el("div", {className:"dlg-actions", style:"justify-content:flex-start"}, save, reset));
}
async function tabProviders(box){
  box.replaceChildren(el("p", {className:"sub", style:"margin:0"},
    "Choose which providers can start agents and how each one is launched. Disabled providers disappear from New agent, the setup wizard and the palette; agents already running keep running. Adding a completely new kind of agent needs an adapter in the program: this panel covers the ones it can drive today."),
    el("div", {className:"skel"}));
  let checks = {};
  try { (await op("provider_check", {})).providers.forEach(c => checks[c.kind] = c); } catch(e){}
  box.replaceChildren(box.firstChild, ...SK.data.providers.map(p => providerCard(p, checks[p.kind])));
}

/* ----- remote hosts (agents on another machine over SSH) ----- */
const HOST_STATE_LABEL = {ok:"reachable", unreachable:"unreachable", reconnecting:"reconnecting", unknown:"not checked yet"};
function hostForm(h, onDone){
  const editing = !!h, id = k => `hf-${k}`;
  const f = (k, label, value, ph, hint, mono = true) => {
    const input = el("input", {className:"input" + (mono ? " mono" : ""), id:id(k), value:value ?? "", placeholder:ph, spellcheck:false, autocomplete:"off", disabled:editing && k === "name"});
    if(k === "identity" || k === "known_hosts") queueMicrotask(() => attachPathComplete(input));
    return el("div", {className:"field"}, el("label", {htmlFor:id(k)}, label), input,
      hint ? el("div", {className:"hint"}, hint) : null);
  };
  const tunnel = el("input", {type:"checkbox", id:id("tunnel"), checked:!!(h && h.tunnel)});
  const err = el("div", {className:"err", role:"alert"});
  const val = k => document.getElementById(id(k)).value.trim();
  const save = el("button", {className:"btn primary", type:"button", onclick:async()=>{
    err.textContent = "";
    const params = {name:val("name"), ssh:val("ssh"), port:Number(val("port") || 22), tunnel:tunnel.checked};
    for(const k of ["identity", "known_hosts", "tmux_socket"]) if(val(k)) params[k] = val(k);
    if(!params.name || !params.ssh){ err.textContent = "A name and a user@address are required"; return; }
    if(!await confirmTyped({title:editing ? `Change host ${params.name}?` : `Add host ${params.name}?`,
      message:`Agents started on this host run commands on ${params.ssh} with the SSH key you set here. The change is audited.`,
      word:"CONFIRM", ok:"Save host"})) return;
    try { await op("host_set", {...params, confirm:"CONFIRM"}); S.meta = null; toast(`Host ${params.name} saved`, "ok"); onDone(); }
    catch(e){ err.textContent = e.message; } }}, editing ? "Save changes" : "Add host");
  return el("form", {className:"prov-card", onsubmit:e=>e.preventDefault(), id:"host-form"},
    el("div", {className:"nm"}, editing ? `Edit ${h.name}` : "Add a remote host"),
    el("div", {className:"onb-row"},
      f("name", "Name", h && h.name, "e.g. build-server", "Short name you pick when creating an agent."),
      f("ssh", "User and address", h && h.ssh, "deploy@192.168.1.20", "user@address (IP or DNS name). A dedicated non-root user is recommended.")),
    el("div", {className:"onb-row"},
      f("port", "SSH port", h ? h.port : 22, "22", "", true),
      f("identity", "Private key file", h && h.identity, "~/.ssh/id_ed25519", "Optional. Must be yours with permissions 0600. Empty = your default SSH keys.")),
    el("details", {className:"adv"}, el("summary", {}, "Advanced"), el("div", {className:"stack"},
      f("known_hosts", "known_hosts file", h && h.known_hosts, "~/.ssh/known_hosts", "Optional: pin the host key in a specific file. Host keys are always verified."),
      f("tmux_socket", "tmux socket name", h && h.tmux_socket, "crewhall", "Name of the tmux server crewhall uses on that machine."))),
    el("div", {className:"field check-row"}, el("label", {className:"check", htmlFor:id("tunnel")}, tunnel, el("span", {}, "Let remote agents message this daemon")),
      el("div", {className:"hint"}, "Opens a reverse SSH tunnel to a restricted gateway (messages, requests and hooks only; no control of this machine). Needs crewhall installed on the remote machine. Off by default.")),
    err, el("div", {className:"dlg-actions", style:"justify-content:flex-start"}, save,
      editing ? el("button", {className:"btn", type:"button", onclick:()=>onDone()}, "Cancel") : null));
}
function hostCard(h, again){
  const result = el("div", {className:"prov-status", id:`host-test-${h.name}`});
  const test = el("button", {className:"btn sm", type:"button", onclick:async()=>{
    result.className = "prov-status"; result.textContent = "Connecting…";
    try {
      const r = await op("host_test", {name:h.name});
      if(r.ok){
        const tick = (ok, what) => `${what} ${ok ? "✓" : "✗"}`;
        result.className = "prov-status " + (r.tmux ? "ok" : "bad");
        result.replaceChildren(r.tmux ? ic("check", "sm") : ic("alert", "sm"),
          ` Connected · ${[tick(r.tmux, "tmux"), tick(r.git, "git"), tick(r.claude, "claude"), tick(r.opencode, "opencode"), tick(r.crewhall, "crewhall")].join("  ")}`
          + (r.tmux ? "" : " — tmux is required"));
      } else { result.className = "prov-status bad"; result.replaceChildren(ic("alert", "sm"), " " + r.error); result.title = r.detail || ""; }
    } catch(e){ result.className = "prov-status bad"; result.textContent = e.message; } }}, "Test connection");
  const edit = el("button", {className:"btn sm", type:"button", onclick:()=>{ SK.editHost = h.name; again(); }}, "Edit");
  const del = el("button", {className:"btn sm danger", type:"button", onclick:async()=>{
    if(!await confirmTyped({title:`Remove host ${h.name}?`, message:"Only the saved connection is removed; nothing on the remote machine is touched. It must have no agents.", word:"CONFIRM", ok:"Remove"})) return;
    try { await op("host_remove", {name:h.name, confirm:"CONFIRM"}); S.meta = null; toast("Host removed", "ok"); again(); } catch(e){ flash(e.message); } }}, "Remove");
  return el("div", {className:"prov-card", dataset:{host:h.name}},
    el("div", {className:"prov-head"}, el("span", {className:"avatar", style:`--h:${hue(h.name)}`}, h.name.charAt(0).toUpperCase()),
      el("div", {style:"flex:1;min-width:0"}, el("div", {className:"nm"}, h.name),
        el("div", {className:"muted mono"}, `${h.ssh}:${h.port}`)),
      el("span", {className:`chip host-${h.state}`}, HOST_STATE_LABEL[h.state] || h.state)),
    el("div", {className:"hint"}, [h.identity ? `key ${h.identity}` : "default SSH keys", h.known_hosts ? `known_hosts ${h.known_hosts}` : null,
      `tmux socket ${h.tmux_socket}`, h.tunnel ? `messaging tunnel ${h.tunnel_state || "starts with the first agent"}` : "messaging tunnel off",
      `${h.agents} agent${h.agents === 1 ? "" : "s"}`].filter(Boolean).join(" · ")),
    result, el("div", {className:"dlg-actions", style:"justify-content:flex-start"}, test, edit, del));
}
async function tabHosts(box){
  let hosts = [];
  try { hosts = (await op("host_list")).hosts; } catch(e){ box.replaceChildren(el("div", {className:"note"}, ic("alert"), e.message)); return; }
  const again = () => { SK.editHost = null; renderSettings(); };
  const editing = hosts.find(h => h.name === SK.editHost);
  box.replaceChildren(el("p", {className:"sub", style:"margin:0"},
    "Run agents on another machine over SSH. Connections use key authentication only and always verify the host key. "
    + "Then choose the machine under “Run on” when you create an agent, and give it a directory on that machine."),
    ...hosts.map(h => editing && editing.name === h.name ? hostForm(h, again) : hostCard(h, again)),
    editing ? null : hostForm(null, again),
    hosts.length ? null : el("div", {className:"hint"}, "Before the first connection, trust the host key once from a terminal: ssh user@address"));
}

/* ----- access & network ----- */
async function tabAccess(box){
  let fe = null, tok = {};
  try { fe = await op("frontend_status"); } catch(e){}
  try { tok = await op("web_token_status"); } catch(e){}
  const parts = [];
  if(fe){
    const modes = [["local", "This computer only", "127.0.0.1, no token needed."], ["tailscale", "Tailscale only", "Your tailnet address; always asks for the token."],
                   ["both", "Both", "Local and Tailscale."]];
    const port = el("input", {className:"input", type:"number", id:"fe-port", min:1, max:65535, value:fe.port, style:"max-width:120px"});
    const radios = modes.map(([m, label, help]) => el("label", {className:"check opt"}, el("input", {type:"radio", name:"fe-mode", value:m, checked:fe.mode === m}),
      el("span", {style:"flex:1"}, el("div", {style:"font-weight:600"}, label), el("div", {className:"muted", style:"font-size:12px"}, help))));
    const status = Object.entries(fe.frontends).map(([n, f]) => el("div", {className:"fe-line " + (f.running ? "ok" : f.error ? "bad" : "")},
      ic(f.running ? "check" : f.error ? "alert" : "x", "sm"), ` ${n}: `, f.running ? f.url : (f.error || "off")));
    parts.push(el("h4", {className:"mc-h"}, "Web UI"), ...radios, el("div", {className:"field"}, el("label", {htmlFor:"fe-port"}, "Port"), port),
      ...status,
      el("div", {className:"dlg-actions", style:"justify-content:flex-start"}, el("button", {className:"btn primary", type:"button", onclick:async()=>{
        const mode = document.querySelector('input[name="fe-mode"]:checked')?.value;
        if(mode !== fe.mode || Number(port.value) !== fe.port){
          if(!await confirmDlg({title:"Change the Web UI?", message:"Connections on a listener that closes are dropped. If you are connected through it, reload from the new address.", ok:"Apply", danger:true})) return;
        }
        try { await op("frontend_set", {mode, port:Number(port.value)}); toast("Web UI updated", "ok"); } catch(e){ flash(e.message); }
        renderSettings(); }}, "Apply"),
        el("span", {className:"muted", style:"font-size:12px"}, "Turning the Web UI off is only possible from the server (crewhall off), so you cannot lock yourself out from here.")));
  }
  parts.push(el("h4", {className:"mc-h"}, "Access token"),
    el("div", {className:"usage-total"},
      el("div", {className:"ucell"}, el("div", {className:"k"}, "Token"), el("div", {className:"v"}, tok.token_exists ? (tok.fingerprint || "set") : "none")),
      el("div", {className:"ucell"}, el("div", {className:"k"}, "File permissions"), el("div", {className:"v"}, tok.file_permissions_ok === false ? "too open" : "private"))),
    el("div", {className:"dlg-actions", style:"justify-content:flex-start"},
      el("button", {className:"btn danger", type:"button", id:"rotateToken", onclick:rotateToken}, ic("refresh", "sm"), "Rotate token…"),
      el("button", {className:"btn", type:"button", onclick:async()=>{
        if(!await confirmDlg({title:"Sign out every device?", message:"All browsers (including this one) must sign in again.", ok:"Sign out all", danger:true})) return;
        try { const r = await op("web_session_revoke"); toast(`${r.revoked} session(s) revoked`, "ok"); } catch(e){ flash(e.message); } }}, "Sign out all devices"),
      el("button", {className:"btn", type:"button", onclick:()=>{ closeSettings(); openAccess(); }}, "Connected devices…")),
    el("div", {className:"hint"}, "Rotating creates a new token (shown once), makes the old one stop working and signs out the other devices. This browser stays signed in."));
  parts.push(el("h4", {className:"mc-h"}, "Authenticator codes (TOTP)"), await totpSection());
  parts.push(el("h4", {className:"mc-h"}, "Passkeys"), await passkeysSection());
  parts.push(el("h4", {className:"mc-h"}, "Terminal tokens"), await terminalTokensSection());
  parts.push(el("h4", {className:"mc-h"}, "Security"), settingsGroup("security", "security"));
  box.replaceChildren(...parts);
}

async function addPasskey(){
  if(!window.PublicKeyCredential){ toast("Passkeys need HTTPS or localhost", "error"); return; }
  try{
    const begin = await (await fetch("/api/webauthn/register/begin", {method:"POST",
      headers:{"Content-Type":"application/json"}, credentials:"same-origin", body:"{}"})).json();
    if(!begin.ok) throw new Error(begin.error || "cannot start");
    const pk = begin.publicKey;
    const cred = await navigator.credentials.create({publicKey:{
      challenge:_wunb64u(pk.challenge), rp:pk.rp,
      user:{id:_wunb64u(pk.user.id), name:pk.user.name, displayName:pk.user.displayName},
      pubKeyCredParams:pk.pubKeyCredParams, timeout:pk.timeout, attestation:pk.attestation,
      authenticatorSelection:pk.authenticatorSelection,
      excludeCredentials:(pk.excludeCredentials||[]).map(c=>({type:"public-key", id:_wunb64u(c.id)})),
    }});
    const body = {ceremony:begin.ceremony, id:_wb64u(cred.rawId),
      clientDataJSON:_wb64u(cred.response.clientDataJSON),
      attestationObject:_wb64u(cred.response.attestationObject),
      label:(document.getElementById("pk-label")||{}).value || "passkey"};
    const fin = await fetch("/api/webauthn/register/finish", {method:"POST",
      headers:{"Content-Type":"application/json"}, credentials:"same-origin", body:JSON.stringify(body)});
    const j = await fin.json().catch(()=>({}));
    if(fin.ok){ toast("Passkey added", "ok"); renderSettings(); }
    else { toast(j.error || "could not add passkey", "error"); }
  }catch(e){ toast(String(e.message || e), "error"); }
}

async function passkeysSection(){
  const wrap = el("div", {className:"tt-sec"});
  let data = {credentials: []};
  try{
    data = await (await fetch("/api/webauthn/credentials", {method:"POST",
      headers:{"Content-Type":"application/json"}, credentials:"same-origin", body:"{}"})).json();
  }catch(e){}
  for(const c of (data.credentials || [])){
    wrap.append(el("div", {className:"tt-row"},
      el("span", {className:"mono"}, c.id), el("span", {}, c.label || ""),
      el("span", {className:"muted"}, c.last_used ? "used" : "new"),
      el("button", {className:"btn sm danger", type:"button", onclick:async()=>{
        if(!await confirmDlg({title:"Remove passkey?", message:c.id, ok:"Remove", danger:true})) return;
        try{ await fetch("/api/webauthn/revoke", {method:"POST",
          headers:{"Content-Type":"application/json"}, credentials:"same-origin",
          body:JSON.stringify({id:c.id})}); toast("Removed", "ok"); renderSettings(); }
        catch(e){ flash(e.message); }
      }}, "Remove")));
  }
  if(!(data.credentials || []).length) wrap.append(el("div", {className:"hint"}, "No passkeys yet."));
  const label = el("input", {className:"input", id:"pk-label", placeholder:"name (e.g. phone)"});
  wrap.append(el("div", {className:"field"}, el("label", {}, "Add a passkey"),
      el("div", {className:"row"}, label,
        el("button", {className:"btn primary", type:"button", onclick:addPasskey}, "Add passkey"))),
    el("div", {className:"hint"}, "Passkeys need a secure context (HTTPS or localhost); "
      + "over plain HTTP the browser does not offer them. The access token still works as a fallback."));
  return wrap;
}

async function totpEnroll(){
  try{
    const label = (document.getElementById("totp-label") || {}).value || "web";
    const r = await (await fetch("/api/totp/begin", {method:"POST",
      headers:{"Content-Type":"application/json"}, credentials:"same-origin",
      body:JSON.stringify({label})})).json();
    if(!r.ok) throw new Error(r.error || "cannot start");
    const box = el("div", {className:"qr-box"});
    if(window.qrcode){ const qr = qrcode(0, "M"); qr.addData(r.uri); qr.make();
      box.innerHTML = qr.createImgTag(4, 8); }
    else { box.textContent = r.uri; }
    const code = el("input", {className:"input", id:"totp-code", inputmode:"numeric",
      maxlength:"7", autocomplete:"one-time-code", placeholder:"000000"});
    const ok = await openDlg(close => el("form", {onsubmit:e=>{ e.preventDefault(); close(true); }},
      el("h3", {}, "Add an authenticator"),
      el("p", {className:"sub"}, "Scan the QR with Authy / Samsung Pass / Google Authenticator, then type the 6-digit code."),
      box, el("div", {className:"hint mono"}, r.secret),
      el("div", {className:"field"}, el("label", {htmlFor:"totp-code"}, "Code"), code),
      el("div", {className:"dlg-actions"},
        el("button", {className:"btn", type:"button", onclick:()=>close(false)}, "Cancel"),
        el("button", {className:"btn primary", type:"submit"}, "Confirm"))));
    if(!ok) return;
    const fin = await fetch("/api/totp/confirm", {method:"POST",
      headers:{"Content-Type":"application/json"}, credentials:"same-origin",
      body:JSON.stringify({ceremony:r.ceremony, code: code.value})});
    const j = await fin.json().catch(()=>({}));
    if(fin.ok){ toast("Authenticator added", "ok"); renderSettings(); }
    else { toast(j.error || "invalid code", "error"); }
  }catch(e){ toast(String(e.message || e), "error"); }
}

async function totpSection(){
  const wrap = el("div", {className:"tt-sec"});
  let data = {enrollments: []};
  try{
    data = await (await fetch("/api/totp/list", {method:"POST",
      headers:{"Content-Type":"application/json"}, credentials:"same-origin", body:"{}"})).json();
  }catch(e){}
  for(const en of (data.enrollments || [])){
    wrap.append(el("div", {className:"tt-row"},
      el("span", {className:"mono"}, en.id), el("span", {}, en.label || ""),
      el("span", {className:"muted"}, en.last_used ? "used" : "new"),
      el("button", {className:"btn sm danger", type:"button", onclick:async()=>{
        if(!await confirmDlg({title:"Remove authenticator?", message:en.id, ok:"Remove", danger:true})) return;
        try{ await fetch("/api/totp/revoke", {method:"POST",
          headers:{"Content-Type":"application/json"}, credentials:"same-origin",
          body:JSON.stringify({id:en.id})}); toast("Removed", "ok"); renderSettings(); }
        catch(e){ flash(e.message); }
      }}, "Remove")));
  }
  if(!(data.enrollments || []).length) wrap.append(el("div", {className:"hint"}, "No authenticator enrolled."));
  const label = el("input", {className:"input", id:"totp-label", placeholder:"name (e.g. phone)"});
  wrap.append(el("div", {className:"field"}, el("label", {}, "Add an authenticator (TOTP)"),
      el("div", {className:"row"}, label,
        el("button", {className:"btn primary", type:"button", onclick:totpEnroll}, "Set up"))),
    el("div", {className:"hint"}, "Codes from Authy / Samsung Pass / Google Authenticator. "
      + "Works over plain HTTP too (unlike passkeys)."));
  return wrap;
}

function parseTtlJs(value){
  const t = String(value || "").trim().toLowerCase();
  if(!t || t === "0") return null;
  const units = {s:1, m:60, h:3600, d:86400};
  const last = t[t.length-1];
  if(units[last]) return Math.round(parseFloat(t.slice(0,-1)) * units[last]);
  return Math.round(parseFloat(t));
}

function showIssuedToken(token, rec){
  openDlg(close => el("form", {onsubmit:e=>{ e.preventDefault(); close(true); }},
    el("h3", {}, "Terminal token"),
    el("p", {className:"sub"}, "Copy it now: it is shown only once."),
    el("input", {className:"input mono", id:"newTermToken", value:token, readOnly:true, onfocus:e=>e.target.select()}),
    el("div", {className:"hint"}, `${(rec.scopes||[]).join(", ")} · hosts ${(rec.hosts||[]).join(",")} · ${rec.id}`),
    el("div", {className:"dlg-actions"}, el("button", {className:"btn primary", type:"submit"}, "Done"))));
}

async function terminalTokensSection(){
  const wrap = el("div", {className:"tt-sec"});
  let data = {tokens: []};
  try { data = await op("terminal_token_list"); } catch(e){}
  const scope = el("select", {className:"select", id:"tt-scope"},
    el("option", {value:"write"}, "write (create & type)"),
    el("option", {value:"read"}, "read (view only)"));
  const hosts = el("input", {className:"input mono", id:"tt-hosts", placeholder:"all, or local,prod1"});
  const ttl = el("input", {className:"input", id:"tt-ttl", value:"8h", style:"max-width:90px"});
  const label = el("input", {className:"input", id:"tt-label", placeholder:"label"});
  const issue = el("button", {className:"btn primary", type:"button", onclick:async()=>{
    try{
      const hv = hosts.value.trim();
      const r = await op("terminal_token_issue", {scope:scope.value,
        hosts: hv ? hv.split(",").map(s=>s.trim()).filter(Boolean) : null,
        ttl: parseTtlJs(ttl.value), label: label.value});
      showIssuedToken(r.token, r.record);
      renderSettings();
    }catch(e){ flash(e.message); }
  }}, "Issue token");
  for(const t of (data.tokens || [])){
    wrap.append(el("div", {className:"tt-row"},
      el("span", {className:"mono"}, t.id),
      el("span", {}, (t.scopes || []).join(",")),
      el("span", {}, (t.hosts || []).join(",")),
      el("span", {className:"muted"}, t.expired ? "expired" : (t.label || "")),
      el("button", {className:"btn sm danger", type:"button", onclick:async()=>{
        if(!await confirmDlg({title:"Revoke token?", message:t.id, ok:"Revoke", danger:true})) return;
        try { await op("terminal_token_revoke", {id:t.id}); toast("Revoked", "ok"); renderSettings(); }
        catch(e){ flash(e.message); } }}, "Revoke")));
  }
  if(!(data.tokens || []).length) wrap.append(el("div", {className:"hint"}, "No terminal tokens yet."));
  wrap.append(el("div", {className:"field"}, el("label", {}, "New terminal token"),
    el("div", {className:"row"}, scope, hosts, ttl, label, issue)),
    el("div", {className:"hint"}, "Creating or typing in a terminal needs a token with scope write; read only allows viewing. The token is limited to the hosts you list. Open the Terminals tab and unlock with a TOTP code (if \u201cTOTP unlocks every terminal\u201d is on) or paste a token there."));
  return wrap;
}
async function rotateToken(){
  if(!await confirmDlg({title:"Rotate the access token?", message:"The current token stops working immediately. Other devices are signed out and need the new token.", ok:"Rotate", danger:true})) return;
  let r;
  try { r = await op("web_token_rotate"); } catch(e){ flash(e.message); return; }
  await openDlg(close => el("form", {onsubmit:e=>{ e.preventDefault(); close(true); }},
    el("h3", {}, "New access token"),
    el("p", {className:"sub"}, `Copy it now: it is shown only once. ${r.signed_out_devices} other device(s) were signed out.`),
    el("input", {className:"input mono", readOnly:true, value:r.token, id:"newToken", onfocus:e=>e.target.select()}),
    el("div", {className:"dlg-actions"},
      el("button", {className:"btn", type:"button", onclick:()=>copyText(r.token, "Token copied")}, ic("copy", "sm"), "Copy"),
      el("button", {className:"btn primary", type:"submit"}, "Done"))));
  renderSettings();
}

/* ----- audit (read-only) ----- */
async function tabAudit(box){
  const opInput = el("input", {className:"input mono", id:"audit-op", placeholder:"filter by operation", spellcheck:false});
  const resultSel = el("select", {className:"input", id:"audit-result"},
    ...[["", "all results"], ["ok", "ok"], ["error", "error"]].map(([v, l]) => el("option", {value:v, textContent:l})));
  const list = el("div", {className:"audit-list"});
  const load = async () => {
    let r;
    try { r = await op("audit_list", {limit:200, filter_op:opInput.value.trim() || null, filter_result:resultSel.value || null}); }
    catch(e){ list.replaceChildren(el("div", {className:"note"}, ic("alert"), e.message)); return; }
    if(!r.events.length){ list.replaceChildren(el("div", {className:"muted"}, "No audit events.")); return; }
    list.replaceChildren(...r.events.map(e => el("div", {className:"audit-row " + (e.result === "ok" ? "ok" : "bad")},
      el("span", {className:"audit-ts mono"}, fmtWhenShort(e.ts)),
      el("span", {className:"audit-op mono"}, e.op),
      el("span", {className:"audit-actor"}, e.actor),
      el("span", {className:"audit-sum"}, e.summary || ""),
      el("span", {className:"audit-res"}, e.result))));
  };
  opInput.oninput = () => load();
  resultSel.onchange = () => load();
  box.replaceChildren(el("p", {className:"sub", style:"margin:0"}, "Privileged operations recorded on this server (logins, token changes, settings, agents, messages). Secret values are never stored."),
    el("div", {className:"row"}, opInput, resultSel, el("button", {className:"btn sm", type:"button", onclick:load}, "Refresh")),
    list);
  await load();
}

/* ----- interface (this browser only) ----- */
function tabInterface(box){
  const theme = store.get("at.theme") || "";
  const sel = el("select", {className:"input", id:"if-theme", onchange:e=>{ const v = e.target.value; if(v){ store.set("at.theme", v); applyTheme(v); } else { try { localStorage.removeItem("at.theme"); } catch(_){} applyTheme(null); } }},
    ...[["", "Follow the system"], ["light", "Light"], ["dark", "Dark"]].map(([v, l]) => el("option", {value:v, textContent:l, selected:v === theme})));
  box.replaceChildren(el("p", {className:"sub", style:"margin:0"}, "These preferences live in this browser only."),
    el("div", {className:"field"}, el("label", {htmlFor:"if-theme"}, "Theme"), sel),
    el("div", {className:"field"}, el("label", {}, "Notifications"),
      el("button", {className:"btn", type:"button", onclick:()=>toggleNotifications()}, ic("bell", "sm"), "Toggle browser notifications"),
      el("div", {className:"hint"}, "Turn on to be told when an agent finishes or needs an answer.")),
    el("div", {className:"field"}, el("label", {}, "Setup wizard"),
      el("button", {className:"btn", type:"button", onclick:()=>{ closeSettings(); openOnboarding(); }}, "Open the setup wizard again")));
}

/* ----- emergency reset ----- */
const RESET_LEVELS = [
  ["clean", "Clean up", "Safe. Drops dead sessions, stale temp files and orphan tmux sessions, and clears caches. Running agents are not touched."],
  ["services", "Restart Web UI", "Everything in Clean up, plus restarts the Web UI listeners and signs every browser out. Agents keep running."],
  ["full", "Full reset", "Stops every agent, clears the temp directory and restarts the daemon in place. A backup is taken first."]];
function tabEmergency(box){
  box.replaceChildren(el("div", {className:"note"}, ic("alert"), "Use these when agents hang, memory or the temp directory fill up, or the UI stops responding. Every level shows exactly what it will do before it does anything. Conversations and your project files are never deleted."),
    ...RESET_LEVELS.map(([lvl, title, text]) => el("div", {className:"reset-card " + lvl, dataset:{level:lvl}},
      el("div", {style:"flex:1;min-width:0"}, el("div", {className:"nm"}, title), el("div", {className:"muted", style:"font-size:12.5px"}, text)),
      el("button", {className:"btn" + (lvl === "full" ? " danger" : ""), type:"button", onclick:()=>previewReset(lvl)}, "Preview…"))));
}
async function previewReset(level){
  let plan;
  try { plan = await op("reset_plan", {level}); } catch(e){ flash(e.message); return; }
  const opts = Object.entries(plan.options || {});
  const word = el("input", {className:"input mono", id:"reset-word", placeholder:`Type ${plan.confirm_word}`, autocomplete:"off", spellcheck:false});
  const boxes = opts.map(([k, label]) => el("input", {type:"checkbox", id:"ro-" + k}));
  const go = el("button", {className:"btn danger solid", type:"submit", disabled:plan.needs_confirm, id:"reset-go"}, level === "full" ? "Reset everything" : "Run");
  if(plan.needs_confirm) word.oninput = () => { go.disabled = word.value.trim() !== plan.confirm_word; };
  const r = await openDlg(close => el("form", {onsubmit:e=>{ e.preventDefault(); if(!go.disabled) close({word:word.value.trim(), opts:Object.fromEntries(opts.map(([k], i) => [k, boxes[i].checked]))}); }},
    el("h3", {}, RESET_LEVELS.find(l => l[0] === level)[1]),
    el("p", {className:"sub"}, "This will:"),
    el("ul", {className:"reset-steps"}, ...plan.steps.map(s => el("li", {}, s.text))),
    plan.live_agents.length ? el("div", {className:"note"}, ic("alert"), `Running agents: ${plan.live_agents.map(a => a.name || a.id).join(", ")}`) : null,
    ...opts.map(([k, label], i) => el("label", {className:"check"}, boxes[i], el("span", {}, label))),
    plan.needs_confirm ? el("div", {className:"field"}, el("label", {htmlFor:"reset-word"}, `Type ${plan.confirm_word} to confirm`), word) : null,
    el("div", {className:"dlg-actions"}, el("button", {className:"btn", type:"button", onclick:()=>close(undefined)}, "Cancel"), go)));
  if(!r) return;
  try {
    const out = await op("reset_apply", {level, confirm:r.word, ...r.opts});
    if(out.restart){ closeSettings(); await waitDaemonBack(out); return; }
    const bad = out.errors.length;
    toast(bad ? `Done with ${bad} problem(s): ${out.errors[0]}` : "Done: " + out.done.slice(-3).join(" · "), bad ? "err" : "ok", 7000);
  } catch(e){ flash(e.message); }
}
/* ----- terminals (raw interactive shells; closed by default) ----- */
async function tabTerminals(view){
  view.replaceChildren(settingsGroup("terminals", "terminals"),
    el("div", {className:"hint"}, "Enabling Web terminals is privileged and asks you to type CONFIRM. "
      + "When disabled, terminal routes return 404 and the CLI exits with code 2."));
}

async function waitDaemonBack(out){
  const note = el("div", {className:"restart-overlay", role:"status"}, el("div", {className:"spinner"}), el("div", {}, "Restarting the daemon…"),
    out.backup ? el("div", {className:"muted"}, "Backup: " + out.backup) : null);
  document.body.append(note);
  const t0 = Date.now();
  while(Date.now() - t0 < 60000){
    await new Promise(r => setTimeout(r, 1500));
    try { const r = await fetch("/api/op?op=ping", {credentials:"same-origin"}); if(r.ok){ const j = await r.json(); if(j.ok !== false){ location.reload(); return; } } } catch(e){}
  }
  note.replaceChildren("The daemon did not come back in 60 s. Check the server: crewhall daemon status");
}

/* ----- shell ----- */
async function renderSettings(){
  const nav = $("settings-nav"), body = $("settings-body");
  nav.replaceChildren(...SET_TABS.map(([k, label]) => el("button", {type:"button", role:"tab", "aria-selected":SK.tab === k, className:SK.tab === k ? "on" : "",
    dataset:{tab:k}, onclick:()=>{ SK.tab = k; renderSettings(); }}, label)));
  if(!SK.data){ body.replaceChildren(el("div", {className:"skel"}), el("div", {className:"skel"})); try { SK.data = await op("settings_get"); } catch(e){ body.replaceChildren(el("div", {className:"note"}, ic("alert"), e.message)); return; } }
  const view = el("div", {className:"set-view", role:"tabpanel"});
  body.replaceChildren(el("h3", {style:"margin:0"}, SET_TABS.find(t => t[0] === SK.tab)[1]), view);
  if(SK.tab === "providers") await tabProviders(view);
  else if(SK.tab === "agents") view.replaceChildren(settingsGroup("agents", "agent"));
  else if(SK.tab === "hosts") await tabHosts(view);
  else if(SK.tab === "terminals") await tabTerminals(view);
  else if(SK.tab === "access") await tabAccess(view);
  else if(SK.tab === "maintenance") view.replaceChildren(settingsGroup("maintenance", "maintenance"),
    el("div", {className:"dlg-actions", style:"justify-content:flex-start"},
      el("button", {className:"btn", type:"button", onclick:()=>{ closeSettings(); openClean(); }}, ic("trash", "sm"), "Open cleanup…"),
      el("button", {className:"btn", type:"button", onclick:()=>{ closeSettings(); openBundle(); }}, ic("folder", "sm"), "Export / import…")),
    el("div", {className:"hint"}, `Settings file: ${SK.data.path}`));
  else if(SK.tab === "interface") tabInterface(view);
  else if(SK.tab === "audit") await tabAudit(view);
  else tabEmergency(view);
}
function openSettings(tab){
  if(tab) SK.tab = tab;
  SK.data = null; S.meta = null;
  const d = $("settingsDlg"); renderSettings(); if(!d.open) d.showModal();
}
function closeSettings(){ const d = $("settingsDlg"); if(d && d.open) d.close(); }
$("settingsBtn").onclick = ()=>openSettings();
$("settingsClose").onclick = closeSettings;
$("settingsDlg").addEventListener("click", e => { if(e.target === $("settingsDlg")) closeSettings(); });
