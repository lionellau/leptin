"""Local memory dashboard — the glass-box audit surface.

A dependency-free HTTP server (stdlib only) that exposes the ledger, the
glass-box memory browser, and compaction/guardrail history, with a single-file
UI embedded below. Run with ``leptin dashboard``.

    GET  /                  -> the UI
    GET  /api/report        -> diet_report (window query param)
    GET  /api/memories      -> memories with effective strength + provenance
    GET  /api/ledger        -> ledger rows (savings over time)
    GET  /api/compactions   -> probe_runs (guardrail pass/fail history)
    POST /api/restore       -> { memory_id }
    POST /api/forget        -> { memory_id }
    POST /api/compact       -> { dry_run }
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from leptin.api import Leptin

_VALID_WINDOWS = {"session", "7d", "all"}


def _memory_views(mem: Leptin, status: str | None) -> list[dict]:
    eng = mem.engine
    rows = mem.store.list_memories(status=None if status in ("all", None) else status)
    out = []
    for m in rows:
        v = eng._public_memory(m)
        v["provenance"] = m.get("provenance")
        v["created_at"] = m.get("created_at")
        v["superseded_by"] = m.get("superseded_by")
        out.append(v)
    return out


def make_handler(mem: Leptin):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def _safe_host(self) -> bool:
            """Reject non-localhost Host headers to blunt DNS-rebinding attacks
            against this local-only dashboard."""
            host = (self.headers.get("Host") or "").split(":")[0].lower()
            return host in ("", "localhost", "127.0.0.1", "::1", "[::1]")

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self):
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._safe_host():
                return self._json({"error": "forbidden host"}, 403)
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)
            if path == "/" or path == "/index.html":
                return self._html()
            if path == "/api/report":
                window = qs.get("window", ["all"])[0]
                if window not in _VALID_WINDOWS:
                    window = "all"
                return self._json(mem.diet_report(window))
            if path == "/api/memories":
                return self._json({"memories": _memory_views(mem, qs.get("status", ["all"])[0])})
            if path == "/api/ledger":
                return self._json({"ledger": mem.store.ledger_rows()})
            if path == "/api/compactions":
                rows = [dict(r) for r in mem.store.conn.execute(
                    "SELECT * FROM probe_runs ORDER BY id DESC LIMIT 50").fetchall()]
                return self._json({"compactions": rows})
            if path == "/api/inspect":
                return self._json(mem.inspect(memory_id=qs.get("memory_id", [None])[0],
                                              query=qs.get("query", [None])[0]))
            if path == "/api/tuning":
                report = mem.diet_report("all").get("tuning")
                return self._json({"tuning": report, "history": mem.tune_history(50)})
            return self._json({"error": "not found"}, 404)

        def _body(self):
            length = int(self.headers.get("Content-Length", 0))
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return {}

        def do_POST(self):
            if not self._safe_host():
                return self._json({"error": "forbidden host"}, 403)
            path = urlparse(self.path).path
            data = self._body()
            if not isinstance(data, dict):
                return self._json({"error": "body must be a JSON object"}, 400)
            mid = data.get("memory_id")
            if path == "/api/restore":
                if not isinstance(mid, str) or not mid:
                    return self._json({"error": "memory_id (string) required"}, 400)
                return self._json(mem.restore(mid))
            if path == "/api/forget":
                q = data.get("query")
                if mid is not None and not isinstance(mid, str):
                    return self._json({"error": "memory_id must be a string"}, 400)
                if q is not None and not isinstance(q, str):
                    return self._json({"error": "query must be a string"}, 400)
                if not mid and not q:
                    return self._json({"error": "memory_id or query required"}, 400)
                return self._json(mem.forget(memory_id=mid, query=q))
            if path == "/api/compact":
                return self._json(mem.compact(dry_run=bool(data.get("dry_run", False))))
            if path == "/api/tune":
                return self._json(mem.tune(dry_run=bool(data.get("dry_run", False))))
            if path == "/api/rollback":
                v = data.get("version")
                return self._json(mem.tune_rollback(version=int(v) if v is not None else None))
            return self._json({"error": "not found"}, 404)

    return Handler


def serve_dashboard(db_path: str, host: str = "127.0.0.1", port: int = 8765) -> None:
    mem = Leptin(db_path)
    handler = make_handler(mem)
    # Single-threaded on purpose: the whole store shares one SQLite connection,
    # and guardrailed compaction runs an explicit transaction — serializing
    # requests keeps that connection safe without locking gymnastics.
    httpd = HTTPServer((host, port), handler)
    url = f"http://{host}:{port}"
    print(f"Leptin dashboard → {url}  (db={mem.store.path})")
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping dashboard")
    finally:
        httpd.server_close()
        mem.close()


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Leptin — memory dashboard</title>
<style>
  :root{
    --bg:#0b0e14; --panel:#121722; --panel2:#0f1420; --line:#1e2636;
    --text:#e6edf3; --muted:#8b98a9; --accent:#3fb950; --accent2:#58a6ff;
    --warn:#f0883e; --danger:#f85149; --chip:#1b2230;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
    font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
  header{padding:24px 28px;border-bottom:1px solid var(--line);display:flex;
    align-items:baseline;gap:14px;flex-wrap:wrap}
  header h1{margin:0;font-size:20px;letter-spacing:.2px}
  header .tag{color:var(--muted);font-size:13px}
  .wrap{padding:24px 28px;max-width:1100px;margin:0 auto}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-bottom:22px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 18px}
  .card .k{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.6px}
  .card .v{font-size:26px;font-weight:650;margin-top:6px}
  .card .v.green{color:var(--accent)} .card .v.blue{color:var(--accent2)}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:22px}
  .panel h2{margin:0 0 14px;font-size:14px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
  th{color:var(--muted);font-weight:600}
  .pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11px;font-weight:600}
  .pill.active{background:#10301b;color:var(--accent)}
  .pill.superseded{background:#2a2233;color:#c08bf0}
  .pill.quarantined{background:#33260f;color:var(--warn)}
  .pill.deleted{background:#33161a;color:var(--danger)}
  .bar{height:6px;background:var(--chip);border-radius:4px;overflow:hidden;width:90px}
  .bar > i{display:block;height:100%;background:linear-gradient(90deg,var(--accent2),var(--accent))}
  button{background:var(--chip);color:var(--text);border:1px solid var(--line);border-radius:7px;
    padding:5px 11px;cursor:pointer;font-size:12px}
  button:hover{border-color:var(--accent2)}
  .controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:14px}
  input,select{background:var(--panel2);color:var(--text);border:1px solid var(--line);
    border-radius:7px;padding:6px 9px;font-size:13px}
  .muted{color:var(--muted)}
  .ok{color:var(--accent)} .bad{color:var(--danger)}
  svg{display:block}
  .empty{color:var(--muted);padding:18px;text-align:center}
</style>
</head>
<body>
<header>
  <h1>🧬 Leptin</h1>
  <span class="tag">personal, local-first memory for your coding agent · audit dashboard</span>
</header>
<div class="wrap">
  <div class="cards" id="cards"></div>

  <div class="panel">
    <h2>Tokens saved over time</h2>
    <div id="chart"></div>
  </div>

  <div class="panel">
    <h2>Compaction &amp; guardrail history</h2>
    <div class="controls">
      <button onclick="compact(true)">Preview compaction</button>
      <button onclick="compact(false)">Run compaction</button>
      <span id="compactMsg" class="muted"></span>
    </div>
    <div id="compactions"></div>
  </div>

  <div class="panel">
    <h2>🧬 Self-tuning (evolution ledger)</h2>
    <div class="controls">
      <button onclick="tune(true)">Preview self-tune</button>
      <button onclick="tune(false)">Self-tune now</button>
      <button onclick="rollback()">Roll back last</button>
      <span id="tuneMsg" class="muted"></span>
    </div>
    <div id="tuning"></div>
  </div>

  <div class="panel">
    <h2>Memory browser (glass box)</h2>
    <div class="controls">
      <input id="search" placeholder="filter by text or subject…" oninput="render()"/>
      <select id="status" onchange="render()">
        <option value="all">all</option>
        <option value="active" selected>active</option>
        <option value="superseded">superseded</option>
        <option value="quarantined">quarantined</option>
      </select>
      <span class="muted" id="memcount"></span>
    </div>
    <div id="memories"></div>
  </div>
</div>
<script>
let MEM=[], LEDGER=[];
const esc=s=>(s==null?'':String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function getJSON(u){const r=await fetch(u);return r.json();}
async function postJSON(u,b){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});return r.json();}

async function load(){
  const rep=await getJSON('/api/report?window=all');
  document.getElementById('cards').innerHTML=`
    <div class="card"><div class="k">Tokens saved</div><div class="v green">${rep.tokens_saved.toLocaleString()}</div></div>
    <div class="card"><div class="k">Est. $ saved</div><div class="v green">$${rep.usd_saved}</div></div>
    <div class="card"><div class="k">Active memories</div><div class="v blue">${rep.active_memories}</div></div>
    <div class="card"><div class="k">Merged</div><div class="v">${rep.ops.merged||0}</div></div>
    <div class="card"><div class="k">Superseded</div><div class="v">${rep.ops.superseded||0}</div></div>
    <div class="card"><div class="k">Decayed</div><div class="v">${rep.ops.decayed||0}</div></div>`;
  LEDGER=(await getJSON('/api/ledger')).ledger;
  drawChart();
  MEM=(await getJSON('/api/memories?status=all')).memories;
  render();
  loadCompactions();
  loadTuning();
}

async function loadTuning(){
  const data=await getJSON('/api/tuning');
  const el=document.getElementById('tuning');
  const t=data.tuning;
  const hist=data.history||[];
  let head='<div class="empty">Self-tuning is off (set self_tune_enabled) or has not run yet.</div>';
  if(t){
    head=`<div class="muted">enabled: <b>${t.enabled}</b> · cycles: ${t.cycles} · accepted: ${t.accepted} · rejected: ${t.rejected} · LLM calls: ${t.llm_calls} (cost-free offline) · current version: ${t.current_version||'—'}</div>`;
  }
  let table='';
  const tuned=hist.filter(h=>h.direction==='tuned'||h.direction==='rollback');
  if(tuned.length){
    table='<table><thead><tr><th>#</th><th>knob(s)</th><th>change</th><th>direction</th><th>reason</th></tr></thead><tbody>'+
      tuned.map(h=>`<tr><td class="muted">${h.id}</td><td>${esc(h.knob||'—')}</td>
        <td class="muted">${esc(JSON.stringify(h.new_value||{}))}</td>
        <td><span class="pill ${h.direction==='rollback'?'superseded':'active'}">${h.direction}</span></td>
        <td class="muted">${esc(h.reason||'')}</td></tr>`).join('')+'</tbody></table>';
  }
  el.innerHTML=head+table;
}

function drawChart(){
  const el=document.getElementById('chart');
  const pts=[];let cum=0;
  LEDGER.forEach(r=>{cum+=r.tokens_saved;pts.push(cum);});
  if(pts.length<2){el.innerHTML='<div class="empty">Run a few remember/recall ops to see savings accumulate.</div>';return;}
  const W=1040,H=140,pad=8,max=Math.max(...pts,1);
  const x=i=>pad+i*(W-2*pad)/(pts.length-1), y=v=>H-pad-v*(H-2*pad)/max;
  let d='M'+pts.map((v,i)=>x(i).toFixed(1)+','+y(v).toFixed(1)).join(' L');
  let area=d+` L${x(pts.length-1).toFixed(1)},${H-pad} L${x(0).toFixed(1)},${H-pad} Z`;
  el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}">
    <defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#3fb95066"/><stop offset="1" stop-color="#3fb95000"/></linearGradient></defs>
    <path d="${area}" fill="url(#g)"/>
    <path d="${d}" fill="none" stroke="#3fb950" stroke-width="2"/>
  </svg><div class="muted">cumulative tokens saved · ${pts[pts.length-1].toLocaleString()} total over ${pts.length} ops</div>`;
}

function render(){
  const q=document.getElementById('search').value.toLowerCase();
  const st=document.getElementById('status').value;
  let rows=MEM.filter(m=>(st==='all'||m.status===st));
  if(q)rows=rows.filter(m=>(m.content||'').toLowerCase().includes(q)||(m.subject||'').toLowerCase().includes(q));
  document.getElementById('memcount').textContent=rows.length+' shown';
  const el=document.getElementById('memories');
  if(!rows.length){el.innerHTML='<div class="empty">No memories yet.</div>';return;}
  el.innerHTML='<table><thead><tr><th>Subject</th><th>Content</th><th>Strength</th><th>Status</th><th>Used</th><th></th></tr></thead><tbody>'+
    rows.map(m=>`<tr>
      <td class="muted">${esc(m.subject||'—')}</td>
      <td>${esc(m.content)}</td>
      <td><div class="bar"><i style="width:${Math.round((m.strength||0)*100)}%"></i></div></td>
      <td><span class="pill ${m.status}">${m.status}</span></td>
      <td class="muted">${m.access_count}×</td>
      <td>${m.status==='active'
        ? `<button onclick="forget('${m.memory_id}')">forget</button>`
        : `<button onclick="restore('${m.memory_id}')">restore</button>`}</td>
    </tr>`).join('')+'</tbody></table>';
}

async function loadCompactions(){
  const rows=(await getJSON('/api/compactions')).compactions;
  const el=document.getElementById('compactions');
  if(!rows.length){el.innerHTML='<div class="empty">No compactions run yet.</div>';return;}
  el.innerHTML='<table><thead><tr><th>Trigger</th><th>Recall before</th><th>Recall after</th><th>Verdict</th></tr></thead><tbody>'+
    rows.map(r=>`<tr>
      <td class="muted">${esc(r.trigger)}</td>
      <td>${(r.recall_before*100).toFixed(0)}%</td>
      <td>${(r.recall_after*100).toFixed(0)}%</td>
      <td>${r.rolled_back?'<span class="bad">rolled back ↩</span>':(r.passed?'<span class="ok">committed ✓</span>':'<span class="bad">failed</span>')}</td>
    </tr>`).join('')+'</tbody></table>';
}

async function restore(id){await postJSON('/api/restore',{memory_id:id});await load();}
async function forget(id){await postJSON('/api/forget',{memory_id:id});await load();}
async function compact(dry){
  const r=await postJSON('/api/compact',{dry_run:dry});
  const g=r.guardrail||{};
  document.getElementById('compactMsg').textContent=
    `${dry?'preview':'ran'}: decayed ${r.decayed}, recall ${(g.recall_before*100||0).toFixed(0)}%→${(g.recall_after*100||0).toFixed(0)}%, `+
    (g.rolled_back?'ROLLED BACK (recall would drop)':(r.dry_run?'would commit':'committed'));
  await load();
}

async function tune(dry){
  const r=await postJSON('/api/tune',{dry_run:dry});
  const ch=(r.changes||[]).map(c=>`${c.knob} ${(+c.old).toFixed(2)}→${(+c.new).toFixed(2)}`).join(', ')||'no change';
  document.getElementById('tuneMsg').textContent=
    `${dry?'preview':'ran'}: ${r.accepted?'ACCEPTED':'no net win'} — ${ch}`+
    (r.objective_after!=null?` (objective ${(r.objective_before).toFixed(3)}→${(r.objective_after).toFixed(3)}, ${r.llm_calls} LLM calls)`:'');
  await load();
}
async function rollback(){
  const r=await postJSON('/api/rollback',{});
  document.getElementById('tuneMsg').textContent=r.rolled_back?`rolled back to version ${r.version}`:`nothing to roll back (${r.reason||''})`;
  await load();
}
load();
</script>
</body>
</html>
"""
