"""The control panel page: one self-contained HTML document (no external libraries, no network)."""

PAGE = r"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AICorp Control Panel</title>
<style>
:root{--bg:#0f1318;--panel:#171d25;--line:#2a3340;--text:#e6ebf2;--mute:#8d99aa;--ok:#3ecf8e;--warn:#f5b84b;--bad:#ff6b6b;--accent:#5aa9ff;--log:#0b0e12}
:root[data-theme=light]{--bg:#f4f6f9;--panel:#fff;--line:#d5dbe3;--text:#18202b;--mute:#5c6878;--ok:#13824f;--warn:#a86a00;--bad:#c62828;--accent:#1a64c8;--log:#fbfcfd}
@media (prefers-color-scheme: light){:root:not([data-theme=dark]){--bg:#f4f6f9;--panel:#fff;--line:#d5dbe3;--text:#18202b;--mute:#5c6878;--ok:#13824f;--warn:#a86a00;--bad:#c62828;--accent:#1a64c8;--log:#fbfcfd}}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--text);font:14px/1.4 system-ui,Segoe UI,Roboto,sans-serif;display:flex;flex-direction:column}
header{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;padding:8px 16px;background:var(--panel);border-bottom:1px solid var(--line)}
h1{font-size:16px;margin:0 8px 0 0}
.group{display:flex;gap:4px;align-items:center}
.seg button,button.btn{background:var(--bg);color:var(--text);border:1px solid var(--line);padding:5px 11px;border-radius:6px;cursor:pointer;font:inherit}
.seg button:first-child{border-radius:6px 0 0 6px}.seg button:last-child{border-radius:0 6px 6px 0}.seg button+button{border-left:0}
.seg button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
button:disabled{opacity:.45;cursor:default}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
button.danger{background:var(--bad);color:#fff;border-color:var(--bad)}
.spacer{flex:1}
.mute{color:var(--mute)}
.chip{display:inline-flex;gap:5px;align-items:center;padding:3px 8px;border:1px solid var(--line);border-radius:12px;font-size:12px}
.dot{width:9px;height:9px;border-radius:50%;background:var(--mute);display:inline-block}
.dot.running{background:var(--ok)}.dot.starting,.dot.stopping{background:var(--warn);animation:pulse 1s infinite}.dot.crashed{background:var(--bad)}
@keyframes pulse{50%{opacity:.3}}
main{flex:1;min-height:0;display:flex;gap:10px;padding:10px 16px 16px}
#doctor{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:10px 16px 0}
#doctor summary{cursor:pointer;font-weight:600}
.check{display:grid;grid-template-columns:70px 1fr auto;gap:6px 10px;padding:6px 0;border-top:1px solid var(--line)}
.check .s{font-weight:700}.s.ok{color:var(--ok)}.s.warn{color:var(--warn)}.s.fail{color:var(--bad)}
.check .step{grid-column:2;color:var(--mute)}
.check .acts{display:flex;gap:6px;align-items:start}
#logs{flex:1;min-width:0;display:grid;gap:8px}
.pane{background:var(--log);border:1px solid var(--line);border-radius:8px;display:flex;flex-direction:column;min-width:0;min-height:120px;overflow:hidden}
.pane h2{margin:0;padding:5px 10px;font-size:12px;text-transform:uppercase;letter-spacing:.05em;background:var(--panel);border-bottom:1px solid var(--line);display:flex;gap:8px;align-items:center}
.pane pre{flex:1;margin:0;padding:8px 10px;overflow:auto;font:12px/1.35 ui-monospace,Consolas,monospace;white-space:pre-wrap;word-break:break-word}
#dash{flex:1;min-width:0;border:1px solid var(--line);border-radius:8px;background:#fff;display:none}
#dash.show{display:block}
#dashmsg{flex:1;display:none;align-items:center;justify-content:center;color:var(--mute);border:1px dashed var(--line);border-radius:8px}
#dashmsg.show{display:flex}
.overlay{position:fixed;inset:0;background:rgba(5,8,12,.78);display:none;align-items:center;justify-content:center;z-index:10}
.overlay.show{display:flex}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px 24px;min-width:min(420px,92vw)}
.card h3{margin:0 0 12px}
.step-row{display:flex;gap:10px;align-items:center;padding:7px 0;opacity:.5;transition:opacity .3s}
.step-row.stopping,.step-row.off,.step-row.failed{opacity:1}
.step-row .ic{width:20px;height:20px;border-radius:50%;border:2px solid var(--line);display:inline-flex;align-items:center;justify-content:center;font-size:12px}
.step-row.stopping .ic{border-color:var(--warn);border-top-color:transparent;animation:spin .8s linear infinite}
.step-row.off .ic{border-color:var(--ok);background:var(--ok);color:#fff;animation:pop .3s}
.step-row.failed .ic{border-color:var(--bad);background:var(--bad);color:#fff}
@keyframes spin{to{transform:rotate(360deg)}}@keyframes pop{0%{transform:scale(.4)}70%{transform:scale(1.25)}100%{transform:scale(1)}}
.row-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:14px}
#toast{position:fixed;bottom:14px;left:50%;transform:translateX(-50%);background:var(--panel);border:1px solid var(--line);padding:8px 14px;border-radius:8px;display:none;z-index:20}
@media (max-width:700px){main{flex-direction:column}.check{grid-template-columns:60px 1fr}.check .acts{grid-column:2}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style>
</head>
<body>
<header>
  <h1>AICorp Control Panel</h1>
  <div class="group seg" id="views" role="group" aria-label="View">
    <button data-v="control" class="on">Control</button><button data-v="dashboard">Dashboard</button><button data-v="split">Split</button>
  </div>
  <div class="group seg" id="layouts" role="group" aria-label="Log layout" title="How the log panes are arranged (view only)">
    <button data-l="single" title="One pane">&#9645;</button><button data-l="cols" title="Side by side">&#9707;</button><button data-l="rows" title="Stacked">&#9636;</button><button data-l="grid" title="Grid" class="on">&#9638;</button>
  </div>
  <div class="group" id="chips"></div>
  <span class="spacer"></span>
  <span class="mute" id="meta"></span>
  <select id="mode" title="fake = simulated work, claude = real Claude Code (uses your Claude plan)"><option value="fake">Simulated work</option><option value="claude">Real Claude</option></select>
  <button class="btn primary" id="onoff">Start</button>
  <button class="btn danger" id="exit">Exit</button>
</header>
<details id="doctor"><summary id="doctorsum">System check</summary><div id="checks"></div>
  <div class="row-actions" style="justify-content:flex-start"><button class="btn" id="recheck">Recheck</button><button class="btn" id="copyall">Copy help prompt for ChatGPT/Claude</button></div></details>
<main>
  <div id="logs"></div>
  <iframe id="dash" title="Dashboard"></iframe>
  <div id="dashmsg">The dashboard is off. Press Start.</div>
</main>
<div class="overlay" id="confirm"><div class="card"><h3>Exit AICorp?</h3><p class="mute">This stops the engine, the watchdog and the dashboard, closes browser windows AICorp opened, and then closes this window. Work in progress is stopped safely and resumes next time.</p>
  <div class="row-actions"><button class="btn" id="no">Cancel</button><button class="btn danger" id="yes">Shut everything down</button></div></div></div>
<div class="overlay" id="shutdown"><div class="card"><h3 id="sdtitle">Shutting down&hellip;</h3><div id="steps"></div><p class="mute" id="sdnote"></p></div></div>
<div id="toast" role="status"></div>
<script>
"use strict";
const TOKEN="__TOKEN__", NAMES=["web","watchdog","engine"], LABEL={web:"Dashboard",watchdog:"Watchdog",engine:"Engine"};
const STEPS=[["engine","Engine (runs the work)"],["watchdog","Watchdog (alerts and chat)"],["web","Dashboard"],["browsers","Browser windows started by AICorp"],["done","Everything is off"]];
const $=s=>document.querySelector(s);
let prefs={view:"control",layout:"grid",hidden:[]}, state=null, promptAll="", prompts={};
try{Object.assign(prefs,JSON.parse(localStorage.getItem("aicorp-ui")||"{}"))}catch(e){}
function save(){try{localStorage.setItem("aicorp-ui",JSON.stringify(prefs))}catch(e){}}
function toast(t){const e=$("#toast");e.textContent=t;e.style.display="block";clearTimeout(toast.t);toast.t=setTimeout(()=>e.style.display="none",4000)}
async function post(url,body){const r=await fetch(url,{method:"POST",headers:{"X-Control-Token":TOKEN,"Content-Type":"application/json"},body:JSON.stringify(body||{})});let j={};try{j=await r.json()}catch(e){}return {ok:r.ok&&j.ok!==false,...j}}

/* ---------- log panes ---------- */
const logs=$("#logs");
NAMES.forEach(n=>{const p=document.createElement("section");p.className="pane";p.id="pane-"+n;
  p.innerHTML=`<h2><span class="dot" id="dot-${n}"></span>${LABEL[n]}<span class="mute" id="note-${n}"></span><span class="spacer"></span><button class="btn" data-restart="${n}">Restart</button></h2><pre id="log-${n}"></pre>`;logs.appendChild(p)});
function addLine(n,line){const pre=$("#log-"+n);const stick=pre.scrollTop+pre.clientHeight>=pre.scrollHeight-24;
  pre.appendChild(document.createTextNode(line+"\n"));while(pre.childNodes.length>500)pre.removeChild(pre.firstChild);if(stick)pre.scrollTop=pre.scrollHeight}
function layoutPanes(){
  const shown=NAMES.filter(n=>!prefs.hidden.includes(n)); const l=prefs.layout;
  NAMES.forEach(n=>$("#pane-"+n).style.display=(shown.includes(n)&&!(l==="single"&&n!==shown[0]))?"flex":"none");
  const k=l==="single"?1:shown.length;
  logs.style.gridTemplateColumns=l==="rows"||l==="single"?"1fr":(l==="cols"?`repeat(${k},1fr)`:(k>2?"1fr 1fr":`repeat(${k},1fr)`));
  logs.style.gridTemplateRows=l==="cols"||l==="single"?"1fr":`repeat(${Math.ceil((l==="rows"?k:l==="grid"?(k>2?2:1):1))},minmax(0,1fr))`;
  document.querySelectorAll("#layouts button").forEach(b=>b.classList.toggle("on",b.dataset.l===l));
  document.querySelectorAll("#chips input").forEach(i=>i.checked=!prefs.hidden.includes(i.value));
}
function applyView(){
  const v=prefs.view, run=state&&state.running;
  logs.style.display=(v==="dashboard")?"none":"grid";
  $("#dash").classList.toggle("show",v!=="control"&&run); $("#dashmsg").classList.toggle("show",v!=="control"&&!run);
  if(v==="split"){logs.style.flex="1";$("#dash").style.flex="1";$("#dashmsg").style.flex="1"}
  if(v==="dashboard"){$("#dash").style.flex="1"}
  document.querySelectorAll("#views button").forEach(b=>b.classList.toggle("on",b.dataset.v===v));
  if(run&&v!=="control"&&$("#dash").dataset.src!==state.dashboard_url){$("#dash").dataset.src=state.dashboard_url;$("#dash").src=state.dashboard_url}
}
const chips=$("#chips");
NAMES.forEach(n=>{const l=document.createElement("label");l.className="chip";l.innerHTML=`<input type="checkbox" value="${n}" checked aria-label="Show ${LABEL[n]} log"><span class="dot" id="chipdot-${n}"></span>${LABEL[n]}`;chips.appendChild(l);
  l.querySelector("input").onchange=e=>{prefs.hidden=e.target.checked?prefs.hidden.filter(x=>x!==n):[...new Set([...prefs.hidden,n])];save();layoutPanes()}});

/* ---------- state ---------- */
function render(){
  if(!state)return; const run=state.running;
  state.processes.forEach(p=>{["dot-","chipdot-"].forEach(pre=>{const d=$("#"+pre+p.name);d.className="dot "+p.state});$("#note-"+p.name).textContent=p.note?" · "+p.note:(p.state==="running"?" · pid "+p.pid:"")});
  $("#onoff").textContent=run?"Stop":"Start"; $("#onoff").classList.toggle("danger",run); $("#onoff").classList.toggle("primary",!run);
  $("#mode").value=state.mode; $("#mode").disabled=run;
  $("#meta").textContent=run?`dashboard port ${state.port}`:`next port ${state.port}`;
  applyView();
}
async function refresh(){const r=await fetch("/api/state");state=await r.json();state.processes.forEach(p=>{const pre=$("#log-"+p.name);if(!pre.dataset.loaded){pre.dataset.loaded=1;(p.lines||[]).forEach(l=>addLine(p.name,l))}});render()}
$("#onoff").onclick=async()=>{ $("#onoff").disabled=true;
  const r=state.running?await post("/api/stop"):await post("/api/start",{mode:$("#mode").value});
  if(!r.ok)toast(r.error||"Could not do that");$("#onoff").disabled=false;setTimeout(refresh,300)};
document.addEventListener("click",async e=>{const n=e.target.dataset&&e.target.dataset.restart;if(n){const r=await post("/api/restart/"+n);if(!r.ok)toast(r.error||"Could not restart")}});
document.querySelectorAll("#views button").forEach(b=>b.onclick=()=>{prefs.view=b.dataset.v;save();applyView()});
document.querySelectorAll("#layouts button").forEach(b=>b.onclick=()=>{prefs.layout=b.dataset.l;save();layoutPanes()});

/* ---------- live stream ---------- */
const stepsEl=$("#steps");
function resetSteps(){stepsEl.innerHTML="";STEPS.forEach(([id,label])=>{const r=document.createElement("div");r.className="step-row";r.id="step-"+id;r.innerHTML=`<span class="ic"></span><span>${label}</span><span class="mute" style="margin-left:auto" id="sd-${id}"></span>`;stepsEl.appendChild(r)})}
function connect(){const es=new EventSource("/api/stream");
  es.onmessage=m=>{const e=JSON.parse(m.data);
    if(e.kind==="log")addLine(e.process,e.line);
    else if(e.kind==="state"){if(state){const p=state.processes.find(x=>x.name===e.process);if(p){p.state=e.state;p.note=e.note||"";if(e.pid)p.pid=e.pid;state.running=state.processes.some(x=>["starting","running","stopping"].includes(x.state));render()}}}
    else if(e.kind==="shutdown"){const row=$("#step-"+e.step);if(!row)return;
      const st=e.state==="stopping"?"stopping":(e.state==="failed"?"failed":"off");row.className="step-row "+st;row.querySelector(".ic").textContent=st==="off"?"✓":(st==="failed"?"!":"");$("#sd-"+e.step).textContent=e.detail||"";
      if(e.step==="done"){$("#sdtitle").textContent=e.detail?"Done, with problems":"All off";$("#sdnote").textContent=exiting?"Closing this window…":"";if(exiting)setTimeout(()=>{window.close();$("#sdnote").textContent="You can close this window now."},900);else setTimeout(()=>$("#shutdown").classList.remove("show"),1200);refresh()}}};
  es.onerror=()=>{if(exiting)$("#sdnote").textContent="The control panel has closed. You can close this window."};}
let exiting=false;
$("#exit").onclick=()=>$("#confirm").classList.add("show");
$("#no").onclick=()=>$("#confirm").classList.remove("show");
$("#yes").onclick=async()=>{$("#confirm").classList.remove("show");exiting=true;resetSteps();$("#sdtitle").textContent="Shutting down…";$("#sdnote").textContent="";$("#shutdown").classList.add("show");const r=await post("/api/exit");if(!r.ok)toast("Exit failed")};

/* ---------- doctor ---------- */
async function copy(text){try{await navigator.clipboard.writeText(text);toast("Copied. Paste it into ChatGPT or Claude.")}catch(e){const t=document.createElement("textarea");t.value=text;document.body.appendChild(t);t.select();try{document.execCommand("copy");toast("Copied.")}catch(x){toast("Could not copy")}t.remove()}}
async function loadDoctor(){const j=await (await fetch("/api/doctor")).json();promptAll=j.prompt;prompts=j.prompts;
  const c=j.summary.counts;$("#doctorsum").textContent=`System check: ${c.ok} ok, ${c.warn} warning(s), ${c.fail} problem(s)`;$("#doctor").open=c.fail>0;
  $("#copyall").style.display=promptAll?"":"none";const box=$("#checks");box.innerHTML="";
  j.checks.forEach(k=>{const d=document.createElement("div");d.className="check";
    d.innerHTML=`<span class="s ${k.status}">${k.status.toUpperCase()}</span><span><b></b> <span class="mute"></span></span><span class="acts"></span>`;
    d.querySelector("b").textContent=k.title;d.querySelector(".mute").textContent=k.detail;
    if(k.status!=="ok"&&k.next_step){const s=document.createElement("div");s.className="step";s.textContent=(k.needs_admin?"You do this yourself: ":"")+k.next_step;d.appendChild(s)}
    const a=d.querySelector(".acts");
    if(k.fix){const b=document.createElement("button");b.className="btn primary";b.textContent="Fix it";b.onclick=async()=>{b.disabled=true;b.textContent="Working…";const r=await post("/api/doctor/fix/"+k.fix);toast(r.message||"Done");loadDoctor()};a.appendChild(b)}
    if(prompts[k.id]){const b=document.createElement("button");b.className="btn";b.textContent="Copy AI prompt";b.onclick=()=>copy(prompts[k.id]);a.appendChild(b)}
    box.appendChild(d)})}
$("#recheck").onclick=loadDoctor; $("#copyall").onclick=()=>copy(promptAll);

layoutPanes(); resetSteps(); refresh().then(()=>{connect();loadDoctor()});
</script>
</body>
</html>
"""
