"use strict";
/* ---------- composer: multiline, prompt history and saved templates ---------- */
S.histIdx = null; S.histDraft = "";
const COMP_MAX = 160;
function composerGrow(){
  const b = $("input"); if(!b || b.tagName !== "TEXTAREA") return;
  b.style.height = "auto";
  b.style.height = Math.min(b.scrollHeight, COMP_MAX) + "px";
}
function historyList(){ try { return JSON.parse(store.get("at.hist") || "[]"); } catch(e){ return []; } }
function pushHistory(text){
  const h = historyList(); h.push(text);
  while(h.length > 50) h.shift();
  store.set("at.hist", JSON.stringify(h));
  S.histIdx = null; S.histDraft = "";
}
function historyNav(dir){
  const b = $("input"), h = historyList();
  if(!h.length) return false;
  if(S.histIdx == null){ if(dir > 0) return false; S.histDraft = b.value; S.histIdx = h.length; }
  let i = S.histIdx + dir;
  if(i < 0) i = 0;
  if(i >= h.length){ S.histIdx = null; b.value = S.histDraft; composerGrow(); return true; }
  S.histIdx = i; b.value = h[i]; composerGrow();
  return true;
}
function templates(){ try { return JSON.parse(store.get("at.tpl") || "[]"); } catch(e){ return []; } }
function saveTemplates(list){ store.set("at.tpl", JSON.stringify(list)); }
function insertTemplate(text){
  const b = $("input"); if(b.disabled) return;
  b.value = b.value.trim() ? b.value.replace(/\s*$/, "") + "\n" + text : text;
  composerGrow(); b.focus();
}
async function savePromptTemplate(){
  const b = $("input"), text = b.value.trim();
  if(!text){ toast("Write a prompt first", "info"); return; }
  const r = await promptDlg({title:"Save prompt as template", ok:"Save",
    fields:[{key:"name", label:"Name", required:true, placeholder:"e.g. Review a diff"}]});
  if(!r) return;
  const list = templates(); list.push({name:r.name, text});
  saveTemplates(list); toast(`Template “${r.name}” saved`, "ok");
}
async function deleteTemplate(){
  const list = templates();
  if(!list.length){ toast("No templates saved", "info"); return; }
  const chosen = await pickDlg({title:"Delete a template", ok:"Delete",
    items:list.map((t, i) => ({id:String(i), label:t.name, sub:t.text.split("\n")[0].slice(0, 60)}))});
  if(!chosen) return;
  const gone = new Set(chosen.map(Number));
  saveTemplates(list.filter((_, i) => !gone.has(i)));
  toast(`${chosen.length} template(s) deleted`, "ok");
}
function openTemplates(anchor){
  const list = templates();
  const items = list.map(t => ({label:t.name, icon:"msg", run:()=>insertTemplate(t.text)}));
  if(list.length) items.push("-");
  items.push({label:"Save current prompt as template…", icon:"plus", run:savePromptTemplate});
  if(list.length) items.push({label:"Delete a template…", icon:"trash", danger:true, run:deleteTemplate});
  openMenu(anchor, items);
}
/* The templates menu is opened from the composer "tools" button (tools.js). */
