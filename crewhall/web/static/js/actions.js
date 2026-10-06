"use strict";
/* ---------- create / delete ---------- */
async function openNewAgent(teamId){
  try { S.meta = S.meta || await fetch("/api/op?op=meta_info").then(r=>r.json()); } catch(e){ S.meta = {}; }
  const kinds = (S.meta.harnesses||[]).map(h=>h.kind);
  const backends = S.meta.backends||["pty"];
  $("na-kind").replaceChildren(...kinds.map(k=>el("option", {textContent:k})));
  const dflt = S.meta.defaults || {};
  if(dflt.kind && kinds.includes(dflt.kind)) $("na-kind").value = dflt.kind;
  const wantBackend = dflt.backend && dflt.backend !== "auto" ? dflt.backend : "tmux";
  $("na-backend").replaceChildren(...backends.map(b=>el("option", {textContent:b, selected:b===wantBackend})));
  $("na-team").replaceChildren(el("option", {value:"", textContent:"(none)"}),
    ...(S.state?.teams||[]).map(t=>el("option", {value:t.team_id, textContent:t.name})));
  if(teamId) $("na-team").value = teamId;   // preselect -> inherits workspace
  let hosts;
  try { hosts = (await op("host_list")).hosts || []; } catch(e){ hosts = S.state?.hosts || []; }
  S.hosts = hosts;   // fresh state for the dialog (the push may be a few seconds old)
  $("na-host").replaceChildren(el("option", {value:"", textContent:"This machine"}),
    ...hosts.map(h => el("option", {value:h.name,
      textContent:`${h.name} — ${h.ssh}${h.port !== 22 ? ":" + h.port : ""} (${h.state})`})));
  $("na-host").value = "";
  updateHostUi();
  updateCwdHint();
  if($("na-workspace-mode")) $("na-workspace-mode").value = "shared";
  $("na-err").textContent=""; $("na-name").value=""; $("na-cwd").value=""; $("na-args").value="";
  $("na-name").classList.remove("invalid"); $("na-ok").classList.remove("loading");
  $("newAgent").showModal(); $("na-name").focus();
}
function updateHostUi(){
  const name = $("na-host").value, hosts = S.hosts || [];
  const h = hosts.find(x => x.name === name);
  const remote = !!name;
  $("na-cwd").dataset.remote = remote ? "1" : "";
  $("na-backend").disabled = remote;
  $("na-cwd-label").textContent = remote ? `Directory on ${name}` : "Working directory";
  $("na-cwd").placeholder = remote ? "/home/user/project (on the remote machine)" : "/absolute/path";
  const hint = $("na-host-hint");
  hint.replaceChildren();
  if(remote){
    if([...$("na-backend").options].some(o => o.value === "ssh-tmux")) $("na-backend").value = "ssh-tmux";
    hint.append(`Runs over SSH on ${h ? h.ssh : name} inside tmux. The agent CLI (claude/opencode) and tmux must be installed there. `,
      h && h.state === "unreachable" ? "The host is not answering right now. " : "");
    document.querySelector("#newAgent details.adv").open = true;
  } else {
    hint.append((S.hosts||[]).length ? "" : "To run an agent on another machine, add it in ",
      (S.hosts||[]).length ? "" : el("a", {href:"#", onclick:(e)=>{ e.preventDefault(); $("newAgent").close(); openSettings("hosts"); }}, "Settings → Remote hosts"),
      (S.hosts||[]).length ? "" : ".");
  }
  updateCwdHint();
}
$("na-host").onchange = updateHostUi;
let _naPath = false;
function updateCwdHint(){
  if(!_naPath){ _naPath = true; attachPathComplete($("na-cwd")); }
  const t = teamById($("na-team").value);
  const hint = $("na-cwd-hint");
  if(hint && $("na-host").value) hint.textContent = "Absolute path on the remote machine. Empty = the SSH user's home directory.";
  else if(hint) hint.textContent = t && t.workspace
    ? `Leave empty to inherit the team workspace: ${t.workspace}`
    : "Leave empty to use the server's working directory";
}
$("na-team").onchange = updateCwdHint;
$("na-cancel").onclick = ()=> $("newAgent").close();
$("newAgent").addEventListener("click", e => { if(e.target === $("newAgent")) $("newAgent").close(); });
async function createAgent(){
  const name = $("na-name").value.trim();
  $("na-name").classList.toggle("invalid", !name);
  if(!name){ $("na-err").textContent="A name is required"; $("na-name").focus(); return; }
  const host = $("na-host").value || null;
  const params = { kind:$("na-kind").value, name, backend:host ? null : $("na-backend").value, host,
                   cwd:$("na-cwd").value.trim()||null, team:$("na-team").value||null,
                   args:$("na-args").value.trim()||null,
                   workspace_mode:$("na-workspace-mode")?.value||null };
  $("na-ok").classList.add("loading"); $("na-err").textContent = "";
  try { const r = await op("agent_create", params); $("newAgent").close();
    toast(`Agent ${name} created`, "ok"); select(r.agent.agent_id); }
  catch(e){ $("na-err").textContent = e.message; }
  finally { $("na-ok").classList.remove("loading"); }
}
$("newAgent").querySelector("form").onsubmit = (e)=>{ e.preventDefault(); createAgent(); };
$("na-ok").type = "submit"; $("na-name").addEventListener("input", ()=>{ $("na-name").classList.remove("invalid"); $("na-err").textContent = ""; });
$("newAgentLink").onclick = ()=>{ openNewAgent(); closeDrawer(); };
$("welcomeNew").onclick = ()=> openNewAgent();
$("newTeamLink").onclick = ()=>{ newTeam(); closeDrawer(); };

async function deleteAgent(id){
  if(!id) return;
  const a = agentById(id);
  if(!await confirmDlg({title:`Delete agent “${a?.name||id}”?`, message:"This stops and unregisters it.", ok:"Delete agent", danger:true})) return;
  try { await op("agent_stop", {target:id});
    if(S.selected===id){ S.selected=null; setView(S.view); }
    toast("Agent deleted", "ok"); render(); } catch(e){ flash(e.message); }
}

async function teamAction(act, teamId){
  const team = teamById(teamId); if(!team) return;
  const all = (S.state.agents||[]);
  const memberIds = new Set((team.members||[]).map(m=>m.agent_id));
  try {
    if(act==="new-agent"){
      // Create an agent directly inside this team; cwd inherits its workspace
      // unless the user provides one.
      await openNewAgent(teamId);
    } else if(act==="add-members"){
      const avail = all.filter(a=>!memberIds.has(a.agent_id));
      if(!avail.length){ toast("No agents available — create one first", "info"); return; }
      const chosen = await pickDlg({title:`Add members to ${team.name}`, ok:"Add",
        items:avail.map(a=>({id:a.agent_id, label:a.name||a.agent_id, sub:a.kind}))});
      if(!chosen) return;
      for(const id of chosen){ await op("team_add_member", {target:teamId, agent:id}); }
      toast(`${chosen.length} member(s) added`, "ok");
    } else if(act==="rm-members"){
      const mem = (team.members||[]);
      if(!mem.length){ toast("The team has no members", "info"); return; }
      const chosen = await pickDlg({title:`Remove members from ${team.name}`, ok:"Remove",
        items:mem.map(m=>({id:m.agent_id, label:m.name||m.agent_id, sub:m.kind}))});
      if(!chosen) return;
      for(const id of chosen){ await op("team_remove_member", {target:teamId, agent:id}); }
      toast(`${chosen.length} member(s) removed`, "ok");
    } else if(act==="set-ws"){
      const r = await promptDlg({title:`Workspace for ${team.name}`, sub:"New agents in this team start in this directory.",
        fields:[{key:"ws", label:"Absolute path", value:team.workspace||"", placeholder:"/home/me/project", mono:true, path:true, hint:"Leave empty to clear the workspace."}]});
      if(!r) return;
      await op("team_set_workspace", {target:teamId, workspace: r.ws||null});
      toast("Workspace updated", "ok");
    } else if(act==="rm-team"){
      if(!await confirmDlg({title:`Delete team “${team.name}”?`, message:"Its agents will NOT be stopped.", ok:"Delete team", danger:true})) return;
      await op("team_remove", {target:teamId});
      toast("Team deleted", "ok");
    }
  } catch(e){ flash(e.message); }
}

async function newTeam(){
  const r = await promptDlg({title:"New team", sub:"Teams let agents message each other and share a workspace.", ok:"Create team",
    fields:[{key:"name", label:"Name", required:true, placeholder:"e.g. backend"},
            {key:"workspace", label:"Workspace (optional)", mono:true, path:true, placeholder:"/absolute/path"}]});
  if(!r) return;
  try { await op("team_create", {name:r.name, agent_ids:[], workspace:r.workspace||null}); toast(`Team ${r.name} created`, "ok"); }
  catch(e){ flash(e.message); }
}

