"use strict";
/* ---------- per-agent timeline: work / wait / error periods and turn durations ----------
 * Built from what the browser observed during this session (state changes over
 * time). It is honest about that: periods before the page opened are not known,
 * so the bar starts at the first observed change, not at midnight.
 */
S.segments = S.segments || {};   // agent_id -> [{state, start, end}]
S.turns = S.turns || {};         // agent_id -> [{start, end, ms}]
const TL_CLASS = {
  working:"working", waiting_input:"waiting", ready:"ready", starting:"starting",
  unknown:"unknown", exited:"exited", error:"error",
};
function tlRecord(agents, now){
  agents.forEach(a => {
    const segs = S.segments[a.agent_id] || (S.segments[a.agent_id] = []);
    const last = segs[segs.length - 1];
    if(!last || last.state !== a.state){
      if(last) last.end = now;
      segs.push({state:a.state, start:now, end:null});
    } else last.end = now;
    // turn duration: working -> not working
    if(a.state === "working"){
      const turns = S.turns[a.agent_id] || (S.turns[a.agent_id] = []);
      const open = turns[turns.length - 1];
      if(!open || open.end){ turns.push({start:now, end:null, ms:0}); }
      else open.ms = now - open.start;
    } else {
      const turns = S.turns[a.agent_id];
      const open = turns && turns[turns.length - 1];
      if(open && !open.end){ open.end = now; open.ms = now - open.start; }
    }
  });
}
function tlSegmentsToday(id){
  const start = new Date(); start.setHours(0, 0, 0, 0);
  const min = start.getTime() / 1000;
  return (S.segments[id] || []).filter(s => (s.end || s.start) >= min);
}
function fmtDur(s){ if(s < 60) return Math.round(s) + "s"; if(s < 3600) return Math.floor(s/60) + "m " + Math.round(s%60) + "s"; return Math.floor(s/3600) + "h " + Math.floor(s%3600/60) + "m"; }
function timelineBar(id){
  const segs = tlSegmentsToday(id);
  const bar = el("div", {className:"tl-bar"});
  if(!segs.length){ bar.append(el("div", {className:"tl-empty"}, "No activity observed yet")); return bar; }
  const t0 = segs[0].start, t1 = Math.max(...segs.map(s => s.end || Date.now()/1000));
  const span = Math.max(1, t1 - t0);
  segs.forEach(s => {
    const dur = (s.end || Date.now()/1000) - s.start;
    const w = Math.max(1, (dur / span) * 100);
    const cls = TL_CLASS[s.state] || "unknown";
    bar.append(el("span", {className:"tl-seg " + cls, style:`width:${w}%`, title:`${stateLabel(s.state)} · ${fmtDur(dur)}`}));
  });
  return bar;
}
function timelineAgent(a){
  const segs = tlSegmentsToday(a.agent_id);
  const total = {working:0, waiting:0, error:0};
  segs.forEach(s => {
    const d = (s.end || Date.now()/1000) - s.start;
    if(s.state === "working") total.working += d;
    else if(s.state === "waiting_input" || s.state === "ready") total.waiting += d;
    else if(s.state === "error") total.error += d;
  });
  const turns = (S.turns[a.agent_id] || []).filter(t => (t.end || t.start) >= (new Date().setHours(0,0,0,0)/1000));
  const last = turns[turns.length - 1];
  const block = el("div", {className:"tl-agent"});
  block.append(el("div", {className:"tl-head"},
    el("span", {className:`avatar ${a.state||"unknown"}`, style:`--h:${hue(a.kind||a.name)}`}, (a.name||a.agent_id||"?").trim().charAt(0) || "?"),
    el("span", {className:"nm"}, a.name||a.agent_id), pill(a)));
  block.append(timelineBar(a.agent_id));
  block.append(el("div", {className:"tl-legend"},
    el("span", {className:"tl-tag working"}, "work ", fmtDur(total.working)),
    el("span", {className:"tl-tag waiting"}, "wait ", fmtDur(total.waiting)),
    total.error ? el("span", {className:"tl-tag error"}, "error ", fmtDur(total.error)) : null,
    turns.length ? el("span", {className:"tl-tag"}, `${turns.length} turn${turns.length===1?"":"s"}`) : null,
    last ? el("span", {className:"tl-tag"}, "last turn ", fmtDur(last.ms/1000)) : null));
  if(turns.length){
    const list = el("div", {className:"tl-turns"});
    turns.slice(-10).reverse().forEach(t => list.append(el("div", {className:"tl-turn"},
      el("span", {className:"dot s-working"}), `${new Date(t.start*1000).toLocaleTimeString()} · ${fmtDur(t.ms/1000)}`)));
    block.append(el("details", {}, el("summary", {}, "Recent turns"), list));
  }
  return block;
}
function renderTimeline(){
  const box = $("timeline-body"); if(!box) return;
  const agents = S.state?.agents || [];
  $("tl-day").textContent = new Date().toLocaleDateString();
  if(!agents.length){ box.replaceChildren(el("div", {className:"none"}, "(no agents)")); return; }
  box.replaceChildren(...agents.map(timelineAgent));
}
function renderTimelineIfOpen(){ if($("timelineDlg") && $("timelineDlg").open) renderTimeline(); }
function openTimeline(){ renderTimeline(); const d = $("timelineDlg"); if(d && !d.open) d.showModal(); }
function closeTimeline(){ const d = $("timelineDlg"); if(d && d.open) d.close(); }
$("timelineBtn").onclick = openTimeline;
$("timelineClose").onclick = closeTimeline;
$("timelineDlg").addEventListener("click", e => { if(e.target === $("timelineDlg")) closeTimeline(); });
