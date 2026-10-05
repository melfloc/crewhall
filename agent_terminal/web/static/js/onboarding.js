"use strict";
/* ---------- first-open onboarding wizard ---------- */
const TEMPLATES = {
  reviewer_executor: {
    label: "Reviewer + executor",
    hint: "A Claude reviewer and an OpenCode executor in one team. The reviewer double-checks the executor's work.",
    team: "trabajo",
    agents: [
      {name: "revisor", kind: "claude", args: "--agent reviewer"},
      {name: "ejecutor", kind: "opencode"},
    ],
  },
  solo: {
    label: "Single agent",
    hint: "One OpenCode agent to start with; add teammates later.",
    team: "trabajo",
    agents: [{name: "agente", kind: "opencode"}],
  },
};
function onboardingDone(){ return store.get("at.onboarded") === "1"; }
function maybeOpenOnboarding(){ if(!onboardingDone()) openOnboarding(); }
async function openOnboarding(){
  let meta = { harnesses: [{ kind: "claude" }, { kind: "opencode" }], backends: ["tmux", "pty"] };
  try { meta = await fetch("/api/op?op=meta_info").then(r => r.json()); } catch(e){}
  const kinds = (meta.harnesses || []).map(h => h.kind);
  const state = el("div", {className:"onb"});
  const pick = {};
  const cards = el("div", {className:"onb-cards"});
  Object.entries(TEMPLATES).forEach(([key, t], i) => {
    pick[key] = i === 0;
    const card = el("div", {className:"onb-card" + (i === 0 ? " on" : ""), role:"button", tabIndex:0,
        onclick:()=>{ Object.keys(pick).forEach(k => pick[k] = k === key); updateOnbCards(cards, key); },
        onkeydown:(e)=>{ if(e.key==="Enter") card.click(); }},
      el("div", {className:"nm"}, t.label), el("div", {className:"pm"}, t.hint),
      el("div", {className:"pm", style:"margin-top:6px"}, t.agents.map(a => `${a.name} (${a.kind})`).join(" · ")));
    cards.append(card);
  });
  const teamName = el("input", {className:"input", value: TEMPLATES.reviewer_executor.team, "aria-label":"Team name"});
  const err = el("div", {className:"err", role:"alert"});
  const backends = meta.backends || ["pty"];
  const backend = el("select", {className:"select"}, ...backends.map(b => el("option", {textContent:b, selected:b==="tmux"})));
  state.append(
    el("h3", {}, "Welcome to crewhall"),
    el("p", {className:"sub"}, "Start a team of coding agents that can talk to each other. Pick a starting point — you can change everything later."),
    cards,
    el("div", {className:"onb-row"},
      el("label", {}, "Team name"), teamName,
      el("label", {}, "Backend"), backend),
    el("div", {className:"hint"}, "Agents start in the server's working directory; set a team workspace later from the team menu."),
    err,
    el("div", {className:"dlg-actions"},
      el("button", {className:"btn", type:"button", onclick:finishOnboarding}, "Skip"),
      el("button", {className:"btn primary", type:"button", onclick:()=>createFromTemplate(pick, teamName.value, backend.value, err)}, "Create team")));
  const d = $("onb"); d.replaceChildren(state);
  if(!d.open) d.showModal();
}
function updateOnbCards(cards, key){ }
function finishOnboarding(){ store.set("at.onboarded", "1"); const d = $("onb"); if(d.open) d.close(); }
async function createFromTemplate(pick, teamName, backend, err){
  const key = Object.keys(pick).find(k => pick[k]);
  const t = TEMPLATES[key]; if(!t) return;
  const name = (teamName || t.team).trim();
  if(!name){ err.textContent = "Team name is required"; return; }
  err.textContent = "";
  try {
    const r = await op("team_up", {spec: {
      name, workspace: null,
      agents: t.agents.map(a => ({name:a.name, kind:a.kind, backend, args:a.args || null})),
    }});
    finishOnboarding();
    toast(`Team “${name}” ready with ${t.agents.length} agent(s)`, "ok");
    const first = (r.created && r.created[0]) || null;
    if(first) select(first);
  } catch(e){ err.textContent = e.message; }
}
$("onb").addEventListener("click", e => { if(e.target === $("onb")) finishOnboarding(); });
