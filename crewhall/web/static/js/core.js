"use strict";
const S = { state:null, selected:null, selectedTerm:null, terminals:[], termView:null,
            follow:true, output:{}, meta:null, view:"live", hist:{},
            q:"", filter:"all", since:{}, prev:{}, doneAt:{}, collapsed:new Set(), proc:{sel:null} };
const $ = (id) => document.getElementById(id);
const NS = "http://www.w3.org/2000/svg";

/* ---------- tiny DOM helpers ---------- */
/* Apply a style string through the CSSOM (setProperty). A strict CSP forbids
   inline `style` attributes, but script-driven style changes are allowed. */
function applyStyle(node, text){
  String(text).split(";").forEach(part => {
    const i = part.indexOf(":");
    if(i <= 0) return;
    const prop = part.slice(0, i).trim(), val = part.slice(i + 1).trim();
    if(prop) { try { node.style.setProperty(prop, val); } catch(e){} }
  });
}
function el(tag, props = {}, ...kids){
  const n = document.createElement(tag);
  for(const [k, v] of Object.entries(props || {})){
    if(v == null || v === false) continue;
    if(k === "dataset") Object.assign(n.dataset, v);
    else if(k === "style" && typeof v === "string") applyStyle(n, v);
    else if(k.includes("-") || k === "role") n.setAttribute(k, v);
    else n[k] = v;
  }
  kids.flat().forEach(k => { if(k != null && k !== false && k !== "") n.append(k); });
  return n;
}
// DOM replaceChildren() turns a null/false child into the text "null"/"false"; drop them.
function setKids(node, ...kids){ node.replaceChildren(...kids.flat().filter(k => k != null && k !== false && k !== "")); }
function ic(name, cls = ""){
  const s = document.createElementNS(NS, "svg"); s.setAttribute("class", "ic " + cls);
  const u = document.createElementNS(NS, "use"); u.setAttribute("href", "#i-" + name); s.append(u); return s;
}
function esc(s){ return String(s??"").replace(/[&<>"]/g, c=>({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;" }[c])); }
const store = {
  get(k){ try { return localStorage.getItem(k); } catch(e){ return null; } },
  set(k, v){ try { localStorage.setItem(k, v); } catch(e){} },
};
const hue = s => { let h = 0; for(const c of String(s||"")) h = (h * 31 + c.charCodeAt(0)) % 360; return h; };
const fmtClock = s => { s = Math.max(0, Math.floor(s)); const h = Math.floor(s/3600), m = Math.floor(s%3600/60), x = s%60;
  return h ? `${h}:${String(m).padStart(2,"0")}:${String(x).padStart(2,"0")}` : `${m}:${String(x).padStart(2,"0")}`; };
const STATE = { ready:"Ready", working:"Working", waiting_input:"Waiting for input", starting:"Starting",
                unknown:"Unknown", exited:"Exited", error:"Error" };
const stateLabel = s => STATE[s] || s || "Unknown";
const dot = (s, kind) => el("span", {className:`dot s-${s||"unknown"}${kind ? " a-" + kind : ""}`, "aria-hidden":"true"});
function timerSpan(a){ const t = el("span", {className:"timer", dataset:{since:a.agent_id}});
  t.textContent = fmtClock((Date.now() - (S.since[a.agent_id]||Date.now()))/1000); return t; }
/* What the agent is doing right now (server-derived from real signals). */
const ACT_ICON = { thinking:"…", tool:"⚙", shell:"$", subagent:"⇉", background:"$", asking:"?" };
function actOf(a){ const x = a.activity; return x && x.kind ? x : {kind:a.state||"unknown", label:stateLabel(a.state), detail:null}; }
function actText(a){ const x = actOf(a); return x.detail ? `${x.label} — ${x.detail}` : x.label; }
function modelOf(a){ return a.activity && a.activity.model || null; }
function pill(a){ const st = a.state||"unknown";
  return el("span", {className:`pill s-${st}`, title:stateLabel(st)}, dot(st, actOf(a).kind), stateLabel(st), st==="working" ? timerSpan(a) : null); }
async function copyText(t, what = "Copied"){
  try { await navigator.clipboard.writeText(t); }
  catch(e){ const ta = el("textarea", {value:t, style:"position:fixed;opacity:0"}); document.body.append(ta); ta.select();
            try { document.execCommand("copy"); } catch(_){} ta.remove(); }
  toast(what, "ok", 1800);
}

/* ---------- toasts / menus / dialogs ---------- */
function toast(msg, kind = "info", ms = 4000){
  const t = el("div", {className:"toast " + kind, role:"status"},
    ic(kind==="err" ? "alert" : kind==="ok" ? "check" : kind==="work" ? "activity" : "msg"), el("span", {}, msg));
  const kill = ()=>{ t.classList.add("out"); setTimeout(()=>t.remove(), 260); };
  t.onclick = kill; $("toasts").append(t);
  while($("toasts").children.length > 4) $("toasts").firstChild.remove();
  setTimeout(kill, ms);
}
function flash(msg){ toast(msg, "err", 6000); }

function closeMenu(){ if(S.menu){ S.menu.remove(); S.menu = null; } }
function openMenu(anchor, items){
  closeMenu();
  const m = el("div", {className:"menu", role:"menu"});
  items.forEach(it => it === "-" ? m.append(el("hr")) : m.append(el("button",
    {className:it.danger ? "danger" : "", role:"menuitem", type:"button",
     onclick:(e)=>{ e.stopPropagation(); closeMenu(); it.run(); }}, ic(it.icon || "chev", "sm"), it.label)));
  // Full keyboard use: arrows move, Home/End jump, Esc closes (Enter/Space activate
  // natively on the buttons).
  m.addEventListener("keydown", (e) => {
    const btns = [...m.querySelectorAll("button")];
    if(!btns.length) return;
    const i = btns.indexOf(document.activeElement);
    if(e.key === "ArrowDown"){ e.preventDefault(); btns[(i + 1 + btns.length) % btns.length].focus(); }
    else if(e.key === "ArrowUp"){ e.preventDefault(); btns[(i - 1 + btns.length) % btns.length].focus(); }
    else if(e.key === "Home"){ e.preventDefault(); btns[0].focus(); }
    else if(e.key === "End"){ e.preventDefault(); btns[btns.length - 1].focus(); }
    else if(e.key === "Escape"){ e.preventDefault(); closeMenu(); anchor.focus(); }
  });
  document.body.append(m); S.menu = m;
  const r = anchor.getBoundingClientRect(), w = m.offsetWidth, h = m.offsetHeight;
  m.style.left = Math.max(8, Math.min(r.right - w, innerWidth - w - 8)) + "px";
  m.style.top = (r.bottom + 4 + h > innerHeight ? Math.max(8, r.top - h - 4) : r.bottom + 4) + "px";
  const f = m.querySelector("button"); if(f) f.focus();
}
document.addEventListener("mousedown", e => { if(S.menu && !S.menu.contains(e.target)) closeMenu(); });
addEventListener("resize", closeMenu);
$("nav").addEventListener("scroll", closeMenu);

/* Directory autocomplete against the daemon's own filesystem. A small ARIA combobox of our
   own (a native <datalist> does not refresh its options while its menu is open, which broke
   the second level). Suggestions end in "/": accepting one and typing on lists the next level.
   Keys: Down/Up move, Enter or Tab accept, Esc closes. Stale answers are discarded. */
let _pathSeq = 0;
function attachPathComplete(input){
  const id = "pathpop-" + (++_pathSeq);
  const pop = el("div", {className:"path-pop", id, role:"listbox", hidden:true});
  input.after(pop);
  input.setAttribute("role", "combobox"); input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-controls", id); input.setAttribute("aria-expanded", "false");
  let timer = null, entries = [], active = -1, ticket = 0;
  const place = () => { const r = input.getBoundingClientRect();
    Object.assign(pop.style, {left:r.left + "px", top:(r.bottom + 2) + "px", width:r.width + "px"}); };
  const close = () => { pop.hidden = true; pop.replaceChildren(); active = -1; input.setAttribute("aria-expanded", "false"); input.removeAttribute("aria-activedescendant"); };
  const paint = () => {
    if(!entries.length){ close(); return; }
    pop.replaceChildren(...entries.map((e, i) => el("div", {className:"path-opt" + (i === active ? " on" : ""), role:"option", id:`${id}-${i}`,
      "aria-selected":i === active, title:e.path, onmousedown:(ev) => { ev.preventDefault(); accept(i); }}, e.path)));
    place(); pop.hidden = false; input.setAttribute("aria-expanded", "true");
    if(active >= 0){ input.setAttribute("aria-activedescendant", `${id}-${active}`); pop.children[active]?.scrollIntoView({block:"nearest"}); }
  };
  const accept = (i) => { const e = entries[i]; if(!e) return;
    input.value = e.path; input.focus(); input.dispatchEvent(new Event("input", {bubbles:true})); };
  const refresh = async () => {
    if(input.dataset.remote === "1"){ entries = []; close(); return; }  // a remote path cannot be completed locally
    const q = input.value, mine = ++ticket;
    try {
      const r = await op("fs_complete", {prefix:q});
      if(mine !== ticket || input.value !== q) return;        // a newer keystroke owns the list
      entries = r.entries || []; active = -1; paint();
    } catch(e){ if(mine === ticket){ entries = []; close(); } }
  };
  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(refresh, 100); });
  input.addEventListener("focus", () => { if(!input.value) refresh(); });
  input.addEventListener("blur", () => setTimeout(close, 120));
  input.addEventListener("keydown", e => {
    if(pop.hidden){ if(e.key === "ArrowDown" && input.value !== undefined){ refresh(); } return; }
    if(e.key === "ArrowDown"){ e.preventDefault(); active = (active + 1) % entries.length; paint(); }
    else if(e.key === "ArrowUp"){ e.preventDefault(); active = (active - 1 + entries.length) % entries.length; paint(); }
    else if(e.key === "Enter" && active >= 0){ e.preventDefault(); e.stopPropagation(); accept(active); }
    else if(e.key === "Tab" && entries.length){ e.preventDefault(); accept(active >= 0 ? active : 0); }
    else if(e.key === "Escape"){ e.stopPropagation(); e.preventDefault(); close(); }
  }, true);
  window.addEventListener("resize", () => { if(!pop.hidden) place(); });
  return input;
}
function openDlg(build){
  return new Promise(resolve => {
    const d = $("dlg"); let done = false;
    const close = v => { if(done) return; done = true; if(d.open) d.close(); resolve(v); };
    d.replaceChildren(build(close));
    // The native `close` event fires asynchronously: if another openDlg() has
    // already reused #dlg by the time it arrives, this handler belongs to that
    // new dialog and must not close it (otherwise chained dialogs — e.g. the
    // confirm → "new token" flow — flash open and close instantly).
    d.onclose = () => { if(!d.open) close(undefined); };
    d.onclick = e => { if(e.target === d) close(undefined); };
    d.showModal();
    const first = d.querySelector("input:not([type=checkbox]),select") || d.querySelector(".btn.primary,.btn.danger");
    if(first) first.focus();
  });
}
const dlgActions = (close, okLabel, danger, okBtn) => el("div", {className:"dlg-actions"},
  el("button", {className:"btn", type:"button", onclick:()=>close(undefined)}, "Cancel"),
  okBtn || el("button", {className:"btn " + (danger ? "danger solid" : "primary"), type:"submit"}, okLabel));
async function confirmDlg({title, message, ok = "Confirm", danger = false}){
  const r = await openDlg(close => el("form", {onsubmit:e=>{ e.preventDefault(); close(true); }},
    el("h3", {}, title), message ? el("p", {className:"sub", style:"margin:0;white-space:pre-wrap;word-break:break-word"}, message) : null,
    dlgActions(close, ok, danger)));
  return r === true;
}
async function promptDlg({title, sub, fields, ok = "Save"}){
  return openDlg(close => {
    // type:"select" fields take options:[{value,label}]; a field with remoteWhen:"<key>" turns into
    // a path *on that host* (no local completion) while the select named <key> has a value.
    const inputs = fields.map(f => f.type === "select"
      ? el("select", {className:"select", id:"pf-" + f.key, disabled:!!f.disabled},
          ...f.options.map(o => el("option", {value:o.value, textContent:o.label, selected:o.value === (f.value||"")})))
      : el("input", {className:"input" + (f.mono ? " mono" : ""), value:f.value||"",
          placeholder:f.placeholder||"", autocomplete:"off", spellcheck:false, id:"pf-" + f.key}));
    fields.forEach((f, i) => { if(f.path) queueMicrotask(() => attachPathComplete(inputs[i])); });
    const labels = fields.map(f => el("label", {htmlFor:"pf-" + f.key}, f.label));
    const sync = () => fields.forEach((f, i) => {
      if(!f.remoteWhen) return;
      const j = fields.findIndex(x => x.key === f.remoteWhen), host = j >= 0 ? inputs[j].value : "";
      inputs[i].dataset.remote = host ? "1" : "";
      labels[i].textContent = host ? (f.remoteLabel || "Directory on the host").replace("{host}", host) : f.label;
      inputs[i].placeholder = host ? (f.remotePlaceholder || "/path/on/the/host") : (f.placeholder || "");
    });
    fields.forEach((f, i) => { if(f.type === "select") inputs[i].onchange = sync; });
    queueMicrotask(sync);
    const err = el("div", {className:"err", role:"alert"});
    return el("form", {onsubmit:e=>{ e.preventDefault();
        const out = {}; for(let i = 0; i < fields.length; i++){ out[fields[i].key] = inputs[i].value.trim();
          if(fields[i].required && !out[fields[i].key]){ err.textContent = fields[i].label + " is required"; inputs[i].classList.add("invalid"); inputs[i].focus(); return; } }
        close(out); }},
      el("h3", {}, title), sub ? el("p", {className:"sub"}, sub) : null,
      ...fields.map((f, i) => el("div", {className:"field"}, labels[i], inputs[i],
        f.hint ? el("div", {className:"hint"}, f.hint) : null)),
      err, dlgActions(close, ok));
  });
}
async function pickDlg({title, sub, items, ok = "Apply"}){
  return openDlg(close => {
    const boxes = items.map(it => el("input", {type:"checkbox", value:it.id}));
    const list = el("div", {className:"pick"}, ...items.map((it, i) => el("label", {className:"check"}, boxes[i],
      el("span", {style:"flex:1;min-width:0"}, el("div", {style:"font-weight:600"}, it.label), it.sub ? el("div", {className:"muted", style:"font-size:12px"}, it.sub) : null))));
    const ok_ = el("button", {className:"btn primary", type:"submit", disabled:true}, ok);
    boxes.forEach(b => b.onchange = () => { const n = boxes.filter(x => x.checked).length; ok_.disabled = !n; ok_.textContent = n ? `${ok} (${n})` : ok; });
    return el("form", {onsubmit:e=>{ e.preventDefault(); close(boxes.filter(b=>b.checked).map(b=>b.value)); }},
      el("h3", {}, title), sub ? el("p", {className:"sub"}, sub) : null, list, dlgActions(close, ok, false, ok_));
  });
}

