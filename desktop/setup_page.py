"""The screen shown before the control panel: checks, problems with steps, settings, and the start progress."""

PAGE = r"""<!doctype html>
<html lang="en" data-theme="dark"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>AICorp</title>
<style>
:root{--bg:#0f1318;--panel:#171d25;--line:#2a3340;--text:#e6ebf2;--mute:#8d99aa;--ok:#3ecf8e;--warn:#f5b84b;--bad:#ff6b6b;--accent:#5aa9ff}
@media (prefers-color-scheme: light){:root{--bg:#f4f6f9;--panel:#fff;--line:#d5dbe3;--text:#18202b;--mute:#5c6878;--ok:#13824f;--warn:#a86a00;--bad:#c62828;--accent:#1a64c8}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,Segoe UI,sans-serif;display:flex;justify-content:center}
main{width:min(760px,100%);padding:28px 20px}
h1{font-size:20px;margin:0 0 4px}.mute{color:var(--mute)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:14px 0}
.check{display:grid;grid-template-columns:64px 1fr auto;gap:4px 10px;padding:7px 0;border-top:1px solid var(--line)}.check:first-of-type{border-top:0}
.s{font-weight:700}.s.ok{color:var(--ok)}.s.warn{color:var(--warn)}.s.fail{color:var(--bad)}
.step{grid-column:2;color:var(--mute)}.acts{display:flex;gap:6px}
button{background:var(--bg);color:var(--text);border:1px solid var(--line);padding:6px 12px;border-radius:6px;font:inherit;cursor:pointer}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}button:disabled{opacity:.45;cursor:default}
label{display:block;margin:8px 0 2px}input,select{width:100%;padding:6px 8px;border-radius:6px;border:1px solid var(--line);background:var(--bg);color:var(--text);font:inherit}
.spin{display:inline-block;width:14px;height:14px;border:2px solid var(--warn);border-top-color:transparent;border-radius:50%;animation:s .8s linear infinite;vertical-align:-2px;margin-right:6px}
@keyframes s{to{transform:rotate(360deg)}}.row{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
pre{white-space:pre-wrap;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px;max-height:180px;overflow:auto;font-size:12px}
#toast{position:fixed;bottom:14px;left:50%;transform:translateX(-50%);background:var(--panel);border:1px solid var(--line);padding:8px 14px;border-radius:8px;display:none}
@media (prefers-reduced-motion:reduce){.spin{animation:none}}
</style></head><body><main>
<h1>AICorp</h1><div class="mute" id="status">Checking your computer&hellip;</div>
<div class="card" id="checks" aria-live="polite"><span class="spin"></span>Please wait. The first check can take a little while if Linux (WSL) has to start.</div>
<div class="row"><button class="primary" id="start" disabled>Start AICorp</button><button id="recheck">Recheck</button><button id="copyall" style="display:none">Copy help prompt for ChatGPT/Claude</button></div>
<div class="card" id="progress" style="display:none"></div>
<details class="card" id="settings"><summary><b>Settings</b></summary>
<label for="distro">Linux distribution (WSL)</label><select id="distro"></select>
<label for="project">AICorp folder inside Linux</label><input id="project" placeholder="e.g. ~/ai-corp" autocomplete="off">
<label for="data">Data folder inside Linux (optional; empty = inside the AICorp folder)</label><input id="data" placeholder="e.g. ~/aicorp-data" autocomplete="off">
<div class="row"><button id="save">Save and recheck</button></div></details>
<div id="toast" role="status"></div>
<script>
"use strict";
const $=s=>document.querySelector(s);let results=[],prompts={},promptAll="",blocked=true;
function toast(t){const e=$("#toast");e.textContent=t;e.style.display="block";clearTimeout(toast.t);toast.t=setTimeout(()=>e.style.display="none",4500)}
async function copy(t){try{await navigator.clipboard.writeText(t);toast("Copied. Paste it into ChatGPT or Claude.")}catch(e){const a=document.createElement("textarea");a.value=t;document.body.appendChild(a);a.select();try{document.execCommand("copy");toast("Copied.")}catch(x){toast("Could not copy")}a.remove()}}
function show(r){results=r.checks;prompts=r.prompts;promptAll=r.prompt;blocked=r.blocked;const box=$("#checks");box.innerHTML="";
  r.checks.forEach(k=>{const d=document.createElement("div");d.className="check";d.innerHTML='<span class="s '+k.status+'"></span><span><b></b> <span class="mute"></span></span><span class="acts"></span>';
    d.querySelector(".s").textContent=k.status.toUpperCase();d.querySelector("b").textContent=k.title;d.querySelector(".mute").textContent=k.detail;
    if(k.status!=="ok"&&k.next_step){const s=document.createElement("div");s.className="step";s.textContent=(k.needs_admin?"You do this yourself: ":"")+k.next_step;d.appendChild(s)}
    const a=d.querySelector(".acts");
    if(k.fix){const b=document.createElement("button");b.className="primary";b.textContent="Fix it";b.onclick=async()=>{b.disabled=true;b.textContent="Working…";const f=await window.pywebview.api.fix(k.fix);toast(f.message);check()};a.appendChild(b)}
    if(prompts[k.id]){const b=document.createElement("button");b.textContent="Copy AI prompt";b.onclick=()=>copy(prompts[k.id]);a.appendChild(b)}
    box.appendChild(d)});
  $("#copyall").style.display=promptAll?"":"none";$("#start").disabled=blocked;
  $("#status").textContent=blocked?"Something needs your attention before AICorp can start.":"Everything needed is in place.";
  const s=r.settings;const sel=$("#distro");sel.innerHTML="";(r.distros.length?r.distros:[s.distro||""]).forEach(n=>{const o=document.createElement("option");o.value=o.textContent=n;sel.appendChild(o)});
  sel.value=s.distro||"";$("#project").value=s.project||"";$("#data").value=s.data_dir||"";$("#settings").open=blocked}
async function check(){$("#start").disabled=true;$("#status").textContent="Checking your computer…";const r=await window.pywebview.api.check();show(r);return r}
async function start(){$("#start").disabled=true;$("#recheck").disabled=true;const p=$("#progress");p.style.display="block";p.innerHTML='<span class="spin"></span><span id="pmsg">Starting Linux (WSL) and AICorp… this can take up to a minute the first time.</span>';
  const r=await window.pywebview.api.start();
  if(!r.ok){p.innerHTML="<b>AICorp could not start.</b> "+(r.error||"")+"<pre></pre>";p.querySelector("pre").textContent=r.log||"";$("#start").disabled=false;$("#recheck").disabled=false}}
$("#start").onclick=start;$("#recheck").onclick=check;$("#copyall").onclick=()=>copy(promptAll);
$("#save").onclick=async()=>{await window.pywebview.api.save({distro:$("#distro").value,project:$("#project").value,data_dir:$("#data").value});check()};
window.addEventListener("pywebviewready",async()=>{const r=await check();if(!r.blocked&&r.autostart)start()});
</script></main></body></html>
"""
