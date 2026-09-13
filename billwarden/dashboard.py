"""BillWarden local dashboard — one page, live event feed, alerts, letters.

Run:  uvicorn billwarden.dashboard:app --port 8619
Open: http://localhost:8619
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .agent import audit_inbox
from .seed import seed
from .tools import EVENTS_PATH, LETTERS_DIR, STATE_PATH, log_event, reset_runtime

app = FastAPI(title="BillWarden")

RUN_STATE = {"running": False, "phase": "idle"}


@app.post("/run")
def run() -> JSONResponse:
    if RUN_STATE["running"]:
        return JSONResponse({"ok": False, "error": "already running"})
    reset_runtime()
    seed(force=True)

    def worker() -> None:
        RUN_STATE["running"] = True
        RUN_STATE["phase"] = "auditing"
        try:
            audit_inbox()
        except Exception as exc:  # pragma: no cover
            log_event("error", f"Audit crashed: {exc}")
        finally:
            RUN_STATE["running"] = False
            RUN_STATE["phase"] = "done"

    threading.Thread(target=worker, daemon=True).start()
    return JSONResponse({"ok": True})


@app.get("/state")
def state() -> JSONResponse:
    events = []
    if EVENTS_PATH.exists():
        events = [json.loads(line) for line in EVENTS_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    st = {}
    if STATE_PATH.exists():
        st = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return JSONResponse({
        "running": RUN_STATE["running"],
        "phase": RUN_STATE["phase"],
        "events": events[-60:],
        "alerts": st.get("alerts", []),
        "digest": st.get("digest", []),
        "letters": st.get("letters", []),
        "summary": st.get("summary", ""),
    })


@app.get("/letter/{name}")
def letter(name: str) -> JSONResponse:
    path = LETTERS_DIR / Path(name).name
    if not path.exists():
        return JSONResponse({"body": "(not found)"})
    return JSONResponse({"body": path.read_text(encoding="utf-8")})


INDEX = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>BillWarden</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--line:#21262d;--fg:#e6edf3;--dim:#8b949e;
--green:#3fb950;--red:#f85149;--amber:#d29922;--blue:#58a6ff}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--fg);font:15px/1.5 "Segoe UI",system-ui,sans-serif;padding:22px}
.wrap{max-width:1180px;margin:0 auto}
header{display:flex;align-items:center;gap:14px;margin-bottom:18px}
.logo{width:40px;height:40px;border-radius:10px;background:linear-gradient(135deg,#1f6feb,#3fb950);
display:flex;align-items:center;justify-content:center;font-size:22px}
h1{font-size:22px;font-weight:650} h1 span{color:var(--dim);font-weight:400;font-size:14px;margin-left:8px}
.badge{margin-left:auto;font-size:12px;color:var(--dim);border:1px solid var(--line);
border-radius:999px;padding:4px 12px}
button.run{background:var(--green);color:#04110a;border:none;border-radius:9px;padding:9px 18px;
font-weight:650;font-size:14px;cursor:pointer}
button.run:disabled{opacity:.5;cursor:default}
.grid{display:grid;grid-template-columns:1fr 1.25fr;gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;min-height:120px}
.card h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);margin-bottom:10px}
.feed{max-height:420px;overflow-y:auto;font-size:13.5px}
.feed div{padding:5px 8px;border-radius:7px;margin-bottom:3px}
.kind-start{color:var(--blue)} .kind-alert{color:var(--red);background:#f8514915;font-weight:600}
.kind-letter{color:var(--amber)} .kind-digest{color:var(--dim)}
.kind-done{color:var(--green);font-weight:650} .kind-inbox,.kind-ledger,.kind-email{color:var(--fg)}
.kind-error{color:var(--red)}
.alert{border-left:3px solid var(--red);background:#f8514910;border-radius:8px;padding:10px 12px;margin-bottom:10px}
.alert.medium{border-left-color:var(--amber);background:#d2992212}
.alert b{display:block} .alert .delta{float:right;color:var(--red);font-weight:700}
.alert.medium .delta{color:var(--amber)}
.digest li{color:var(--dim);font-size:13.5px;margin:5px 0 5px 16px}
.letterrow{display:flex;justify-content:space-between;padding:8px 4px;border-bottom:1px solid var(--line);
cursor:pointer;font-size:14px} .letterrow:hover{color:var(--blue)}
.summary{margin-top:16px;background:#1f6feb15;border:1px solid #1f6feb40;border-radius:12px;padding:14px 16px}
.total{font-size:26px;font-weight:750;color:var(--red)}
.total span{font-size:13px;color:var(--dim);font-weight:400}
#lettermodal{position:fixed;inset:0;background:#000a;display:none;align-items:center;justify-content:center}
#lettermodal pre{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:22px;
max-width:640px;white-space:pre-wrap;font:13.5px/1.6 Consolas,monospace;cursor:pointer}
.idle{color:var(--dim);font-size:13.5px;margin-top:6px}
</style></head>
<body><div class="wrap">
<header>
  <div class="logo">🛡️</div>
  <h1>BillWarden <span>household bill auditor — runs locally, pings only when a decision is needed</span></h1>
  <button class="run" id="runbtn" onclick="runAudit()">▶ Run weekly audit</button>
  <div class="badge">Strands Agent · offline rule judge · pluggable LLM provider</div>
</header>
<div class="grid">
  <div>
    <div class="card"><h2>Agent activity — live</h2><div class="feed" id="feed"><div class="idle">Press “Run weekly audit”. 6 new billing emails are waiting in the demo inbox.</div></div></div>
    <div class="card" style="margin-top:16px"><h2>Quiet digest — no ping needed</h2><ul class="digest" id="digest"><li class="idle">nothing yet</li></ul></div>
  </div>
  <div>
    <div class="card"><h2>Decisions waiting for the household</h2>
      <div class="total" id="leak">$0.00 <span>/ month currently leaking</span></div>
      <div id="alerts" style="margin-top:12px"><div class="idle">no alerts yet</div></div>
    </div>
    <div class="card" style="margin-top:16px"><h2>Draft letters — ready to send</h2><div id="letters"><div class="idle">none yet</div></div></div>
    <div class="summary" id="summarybox" style="display:none"><b>Weekly summary</b><p id="summary"></p></div>
  </div>
</div>
<div id="lettermodal" onclick="this.style.display='none'"><pre id="letterbody"></pre></div>
</div>
<script>
async function runAudit(){
  document.getElementById('runbtn').disabled=true;
  document.getElementById('feed').innerHTML='';
  await fetch('/run',{method:'POST'});
}
async function showLetter(file){
  const r=await (await fetch('/letter/'+file)).json();
  document.getElementById('letterbody').textContent=r.body;
  document.getElementById('lettermodal').style.display='flex';
}
function esc(s){return (s||'').replace(/</g,'&lt;')}
setInterval(async()=>{
  const st=await (await fetch('/state')).json();
  const feed=document.getElementById('feed');
  if(st.events.length){feed.innerHTML=st.events.map(e=>`<div class="kind-${e.kind}">${esc(e.message)}</div>`).join('');feed.scrollTop=feed.scrollHeight;}
  document.getElementById('runbtn').disabled=st.running;
  const alerts=st.alerts||[];
  document.getElementById('leak').innerHTML=`$${(st.alerts||[]).reduce((s,a)=>s+a.amount_delta,0).toFixed(2)} <span>/ month currently leaking</span>`;
  document.getElementById('alerts').innerHTML=alerts.length?alerts.map(a=>
    `<div class="alert ${a.severity==='high'?'':'medium'}"><span class="delta">$${a.amount_delta.toFixed(2)}/mo</span><b>${esc(a.vendor)} — ${esc(a.title)}</b>${esc(a.reason)}</div>`).join(''):'<div class="idle">no alerts yet</div>';
  document.getElementById('digest').innerHTML=(st.digest||[]).length?st.digest.map(n=>`<li>${esc(n)}</li>`).join(''):'<li class="idle">nothing yet</li>';
  document.getElementById('letters').innerHTML=(st.letters||[]).length?st.letters.map(l=>
    `<div class="letterrow" onclick="showLetter('${esc(l.file)}')"><span>✉️ ${esc(l.subject)}</span><span>${esc(l.vendor)}</span></div>`).join(''):'<div class="idle">none yet</div>';
  if(st.summary){document.getElementById('summarybox').style.display='block';document.getElementById('summary').textContent=st.summary;}
},1000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(INDEX)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8619)
