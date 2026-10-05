"use strict";
/* ---------- usage: tokens, estimated cost and active time (from real signals) ---------- */
const ND = "n/d";
function fmtTokens(n){
  if(n == null) return ND;
  if(n < 1000) return String(n);
  if(n < 1_000_000) return (n/1000).toFixed(n < 10000 ? 1 : 0) + "K";
  return (n/1_000_000).toFixed(2) + "M";
}
function fmtCost(v){ return v == null ? ND : "$" + Number(v).toFixed(4).replace(/0+$/, "").replace(/\.$/, ""); }
function activeSeconds(a){
  // Time the page has observed this agent, not a lifetime counter.
  const segs = (S.segments || {})[a.agent_id] || [];
  const day = new Date().setHours(0, 0, 0, 0) / 1000;
  return segs.filter(s => (s.end || s.start) >= day).reduce((t, s) => t + ((s.end || Date.now()/1000) - s.start), 0);
}
function usageOf(a){ return (a.usage && a.usage.available) ? a.usage : null; }
function usageRow(a){
  const u = usageOf(a);
  const row = el("div", {className:"usage-row"});
  row.append(el("div", {className:"usage-head"},
    el("span", {className:`avatar ${a.state||"unknown"}`, style:`--h:${hue(a.kind||a.name)}`}, (a.name||a.agent_id||"?").trim().charAt(0) || "?"),
    el("span", {className:"nm"}, a.name||a.agent_id), el("span", {className:"chip mono"}, a.kind), pill(a)));
  const cells = el("div", {className:"usage-cells"},
    el("div", {className:"ucell"}, el("div", {className:"k"}, "Tokens"), el("div", {className:"v"}, u ? (u.tokens_estimated ? "~" : "") + fmtTokens(u.tokens) : ND)),
    el("div", {className:"ucell"}, el("div", {className:"k"}, "Cost"), el("div", {className:"v"}, u ? fmtCost(u.cost_usd) : ND)),
    el("div", {className:"ucell"}, el("div", {className:"k"}, "Active today"), el("div", {className:"v"}, fmtDur(activeSeconds(a)))));
  row.append(cells);
  if(!u) row.append(el("div", {className:"usage-note"}, "This agent does not report tokens/cost yet — shown as n/d."));
  else if(u.tokens_estimated) row.append(el("div", {className:"usage-note"}, "Tokens are the context size the CLI rounds (" + u.source + ")."));
  return row;
}
function renderUsageIfOpen(){ if($("usageDlg") && $("usageDlg").open) renderUsage(); }
function renderUsage(){
  const box = $("usage-body"); if(!box) return;
  const agents = S.state?.agents || [];
  const totals = agents.reduce((t, a) => {
    const u = usageOf(a);
    if(u){ t.tokens += u.tokens || 0; t.cost += u.cost_usd || 0; }
    else t.unknown++;
    t.active += activeSeconds(a);
    return t;
  }, {tokens:0, cost:0, active:0, unknown:0});
  const total = el("div", {className:"usage-total"},
    el("div", {className:"ucell"}, el("div", {className:"k"}, "Total tokens"), el("div", {className:"v"}, totals.tokens ? "~" + fmtTokens(totals.tokens) : ND)),
    el("div", {className:"ucell"}, el("div", {className:"k"}, "Total cost"), el("div", {className:"v"}, totals.cost ? fmtCost(totals.cost) : ND)),
    el("div", {className:"ucell"}, el("div", {className:"k"}, "Active today"), el("div", {className:"v"}, fmtDur(totals.active))),
    totals.unknown ? el("div", {className:"ucell"}, el("div", {className:"k"}, "No data"), el("div", {className:"v"}, totals.unknown + " agent(s)")) : null);
  const team = (S.state?.teams || []).map(t => {
    const mem = (t.members||[]).map(m => agentById(m.agent_id)).filter(Boolean);
    const tok = mem.reduce((n, a) => n + ((usageOf(a)||{}).tokens || 0), 0);
    const cost = mem.reduce((n, a) => n + ((usageOf(a)||{}).cost_usd || 0), 0);
    return el("div", {className:"usage-team"}, el("span", {className:"nm"}, t.name),
      el("span", {className:"ucell"}, "~" + fmtTokens(tok) + " tokens"), el("span", {className:"ucell"}, fmtCost(cost)));
  });
  box.replaceChildren(
    el("h4", {className:"mc-h"}, "This session", el("span", {className:"chip"}, agents.length)),
    total,
    el("h4", {className:"mc-h"}, "Per agent"),
    ...agents.map(usageRow),
    ...(team.length ? [el("h4", {className:"mc-h"}, "Per team"), ...team] : []));
}
function openUsage(){ renderUsage(); const d = $("usageDlg"); if(d && !d.open) d.showModal(); }
function closeUsage(){ const d = $("usageDlg"); if(d && d.open) d.close(); }
$("usageBtn").onclick = openUsage;
$("usageClose").onclick = closeUsage;
$("usageDlg").addEventListener("click", e => { if(e.target === $("usageDlg")) closeUsage(); });
