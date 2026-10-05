"use strict";
/* ---------- cleanup: temp leftovers, old backups, old releases (dry-run first) ---------- */
function fmtBytes(n){
  n = Number(n) || 0;
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while(n >= 1024 && i < units.length - 1){ n /= 1024; i++; }
  return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + " " + units[i];
}
const CLEAN_KIND = { tmp:"Test leftovers", "control-backup":"Control backups", "control-backup-dir":"Empty backup dirs",
                     "update-backup":"Update backups", release:"Old releases" };
async function openClean(){
  let plan;
  try { plan = await op("clean_plan"); }
  catch(e){ flash(e.message); return; }
  const grouped = {};
  (plan.items || []).forEach(i => { (grouped[i.kind] = grouped[i.kind] || []).push(i); });
  const list = el("div", {className:"clean-list"});
  if(!plan.items || !plan.items.length) list.append(el("div", {className:"none"}, "Nothing to clean."));
  Object.keys(grouped).forEach(kind => {
    const items = grouped[kind];
    const bytes = items.reduce((n, i) => n + i.bytes, 0);
    list.append(el("h4", {className:"mc-h"}, CLEAN_KIND[kind] || kind,
      el("span", {className:"chip"}, `${items.length} · ${fmtBytes(bytes)}`)));
    const ul = el("div", {className:"clean-items"});
    items.slice(0, 40).forEach(i => ul.append(el("div", {className:"clean-item", title:i.path, textContent:i.path})));
    if(items.length > 40) ul.append(el("div", {className:"clean-more"}, `… and ${items.length - 40} more`));
    list.append(ul);
  });
  const total = el("div", {className:"clean-total"},
    `Everything listed is a temporary/backup artifact; live agents and current releases are never touched.`);
  const remove = el("button", {className:"btn danger solid", type:"button", disabled:!plan.items || !plan.items.length},
    `Remove ${plan.items ? plan.items.length : 0} item(s) · ${fmtBytes(plan.total_bytes || 0)}`);
  const dlg = el("div", {className:"dlg-body"}, el("h3", {}, "Clean up"), total, list,
    el("div", {className:"dlg-actions"},
      el("button", {className:"btn", type:"button", onclick:()=>closeDlg()}, "Cancel"), remove));
  remove.onclick = async () => {
    remove.disabled = true;
    if(!await confirmDlg({title:"Remove these items?", message:"This deletes the listed temporary and backup files. It cannot be undone.",
        ok:"Remove", danger:true})){ remove.disabled = false; return; }
    try { const r = await op("clean_apply"); closeDlg(); toast(`Removed ${r.removed.length} item(s), ${fmtBytes(r.bytes)}`, "ok"); }
    catch(e){ flash(e.message); remove.disabled = false; }
  };
  showDlgNode(dlg);
}
function showDlgNode(node){
  const d = $("dlg"); d.replaceChildren(node);
  d.onclose = null; d.onclick = e => { if(e.target === d) closeDlg(); };
  if(!d.open) d.showModal();
}
function closeDlg(){ const d = $("dlg"); if(d && d.open) d.close(); }
$("cleanBtn").onclick = openClean;
