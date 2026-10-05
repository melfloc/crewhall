"use strict";
/* ---------- export / import the team configuration as a bundle ---------- */
function fmtWhenShort(ts){ return new Date(ts * 1000).toLocaleString(); }
async function openBundle(){
  const body = el("div", {className:"dlg-body"},
    el("h3", {}, "Export / import configuration"),
    el("p", {className:"sub", style:"margin:0"}, "A bundle saves your profiles, team files and the teams and agents you have now, as definitions you can rebuild on another machine (no tokens, no conversations). It is stored on the server: download it to move it to another machine, and upload it there to import it."),
    el("div", {className:"bundle-actions"},
      el("button", {className:"btn primary", type:"button", id:"bundleExport"}, ic("down", "sm"), "Export now"),
      el("button", {className:"btn", type:"button", id:"bundleUpload", onclick:()=>$("bundleFile").click()}, ic("plus", "sm"), "Upload bundle…"),
      el("input", {type:"file", id:"bundleFile", accept:".gz,.tar.gz,application/gzip", style:"display:none", "aria-label":"Bundle file",
        onchange:(e)=>{ const f = e.target.files[0]; e.target.value = ""; if(f) uploadBundle(f); }}),
      el("button", {className:"btn danger", type:"button", id:"bundleClean", style:"display:none", onclick:cleanJunkBundles}, ic("trash", "sm"), "Delete empty / invalid")),
    el("h4", {className:"mc-h"}, "Saved bundles"),
    el("div", {className:"bundle-list", id:"bundleList"}, el("div", {className:"skel"})));
  const d = $("dlg"); d.replaceChildren(body);
  d.onclose = null; d.onclick = e => { if(e.target === d && d.open) d.close(); };
  if(!d.open) d.showModal();
  $("bundleExport").onclick = async () => {
    try { const r = await op("bundle_export"); toast(`Exported ${r.name}`, "ok"); refreshBundleList(); }
    catch(e){ flash(e.message); }
  };
  refreshBundleList();
}
const isJunk = b => !b.valid || b.empty;
async function refreshBundleList(){
  let r;
  try { r = await op("bundle_list"); } catch(e){ return; }
  const list = $("bundleList"); if(!list) return;
  S.bundles = r.bundles;
  const junk = r.bundles.filter(isJunk);
  const cleanBtn = $("bundleClean");
  if(cleanBtn){ cleanBtn.style.display = junk.length ? "" : "none"; cleanBtn.lastChild.textContent = `Delete empty / invalid (${junk.length})`; }
  if(!r.bundles.length){ list.replaceChildren(el("div", {className:"none"}, "No bundles yet.")); return; }
  list.replaceChildren(...r.bundles.slice().reverse().map(b => el("div", {className:"bundle-row" + (isJunk(b) ? " junk" : ""), dataset:{bundle:b.name}},
    ic("folder", "sm"),
    el("div", {style:"min-width:0;flex:1"}, el("div", {className:"nm", title:b.name}, b.name),
      el("div", {className:"pm", style:"margin:0"}, fmtWhenShort(b.mtime), " · ", fmtBytes(b.bytes), " · ",
        !b.valid ? el("span", {className:"bad", title:b.error}, "invalid: " + (b.error || "unreadable"))
        : b.empty ? el("span", {className:"bad"}, "empty — nothing to restore")
        : `${b.files} file(s), ${b.teams} team(s)`,
        b.has_token ? el("span", {className:"bad", title:"Contains your web access token"}, " · contains token") : null)),
    el("a", {className:"btn sm", href:"/api/bundle/" + encodeURIComponent(b.name), download:b.name, title:"Download this bundle"}, ic("down", "sm"), "Download"),
    b.valid && !b.empty ? el("button", {className:"btn sm", type:"button", onclick:()=>importBundle(b.name)}, "Import…") : null,
    el("button", {className:"btn icon sm ghost", type:"button", "aria-label":`Delete ${b.name}`, title:"Delete this bundle",
      onclick:()=>deleteBundles([b.name])}, ic("trash", "sm")))));
}
async function uploadBundle(file){
  try {
    const res = await fetch("/api/bundle?name=" + encodeURIComponent(file.name), {method:"POST", body:file, credentials:"same-origin",
      headers:{"Content-Type":"application/gzip"}});
    const j = await res.json().catch(() => ({}));
    if(!res.ok || !j.ok) throw new Error(j.error || `upload failed (${res.status})`);
    toast(`Uploaded ${j.name}`, "ok"); refreshBundleList();
  } catch(e){ flash(e.message); }
}
async function deleteBundles(names){
  const many = names.length > 1;
  if(!await confirmDlg({title:many ? `Delete ${names.length} bundles?` : "Delete this bundle?",
      message:many ? names.join("\n") : names[0], ok:"Delete", danger:true})){ openBundle(); return; }
  try { const r = await op("bundle_delete", {names});
        toast(`Deleted ${r.deleted.length} bundle(s)` + (r.failed.length ? `, ${r.failed.length} failed` : ""), r.failed.length ? "err" : "ok"); }
  catch(e){ flash(e.message); }
  openBundle();
}
function cleanJunkBundles(){ const names = (S.bundles || []).filter(isJunk).map(b => b.name); if(names.length) deleteBundles(names); }
async function importBundle(name){
  let preview;
  try { preview = await op("bundle_import", {name, dry_run:true}); }
  catch(e){ flash(e.message); return; }
  const plan = preview.teams_plan || [], agents = plan.flatMap(t => t.agents);
  const missing = agents.filter(a => !a.cwd_ok);
  const lines = [preview.written.length ? `${preview.written.length} file(s) would be written.` : "No files to write.",
                 `Skipped: ${preview.skipped.length} (existing files are kept).`];
  plan.forEach(t => lines.push(`Team ${t.team}: ` + t.agents.map(a => a.name + (a.cwd_ok ? "" : " ⚠")).join(", ")));
  if(missing.length) lines.push(`⚠ ${missing.length} agent(s) have no directory on this machine and will be skipped.`);
  const apply = agents.length > 0;
  if(!await confirmDlg({title:`Import ${name}?`, message:lines.join("\n") + (apply ? "\nThe teams and agents above will be created (existing ones are kept)." : ""),
      ok:apply ? "Import and create" : "Import", danger:true})) return;
  try { const r = await op("bundle_import", {name, dry_run:false, force:false, apply_teams:apply});
        const t = r.teams_applied;
        toast(`Imported ${r.written.length} file(s)` + (t ? ` · ${t.created.length} agent(s) created` + (t.skipped.length ? `, ${t.skipped.length} skipped` : "") : ""), "ok");
        closeBundle(); }
  catch(e){ flash(e.message); }
}
function closeBundle(){ const d = $("dlg"); if(d && d.open) d.close(); }
$("bundleLink").onclick = ()=>{ openBundle(); closeDrawer(); };
