"""Dashboard — FastAPI app on port 8000.

The dashboard is the ONLY writer to control.db.
It reads ledger.db read-only (mode=ro).
Why: single-writer rule prevents SQLite lock contention.
"""
from fastapi import FastAPI, Request, Form, BackgroundTasks
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel
import sqlite3
import json
import time
from pathlib import Path
from typing import Optional, Dict, Any

from ledgerhand.control.presets import PRESETS

app = FastAPI()

templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

def init_control_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute('''
        CREATE TABLE IF NOT EXISTS commands(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            kind TEXT,
            payload_json TEXT,
            created_at TEXT
        )
    ''')
    conn.commit()
    return conn

# Setup DBs
runs_dir = Path("runs")
runs_dir.mkdir(exist_ok=True)
control_db_path = runs_dir / "control.db"
control_conn = init_control_db(control_db_path)
ledger_db_path = runs_dir / "ledger.db"

def get_ledger_conn():
    if not ledger_db_path.exists():
        return None
    uri = f"file:{ledger_db_path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn

@app.get("/")
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "presets": PRESETS})

@app.post("/goal")
async def submit_goal(request: Request, goal_text: str = Form(...),
                      inbox: str = Form(default="data/batch_a")):
    """Accept a natural-language goal from the dashboard form.

    Single-writer rule: the dashboard writes ONLY to control.db.
    It inserts a 'run_goal' command with the goal_text and inbox path.
    The runner (ledgerhand run / ledgerhand resume) polls control.db for
    this command and starts the job.

    Why not launch a subprocess here: that would make the dashboard a
    second writer to ledger.db, violating the single-writer rule.
    Instead, the human operator launches 'ledgerhand run' separately,
    and the dashboard just queues the goal.
    """
    import uuid
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # run_id is a placeholder until the CLI creates the real run
    pending_run_id = f"pending_{uuid.uuid4().hex[:8]}"
    payload = json.dumps({"goal_text": goal_text, "inbox": inbox})
    control_conn.execute(
        "INSERT INTO commands (run_id, kind, payload_json, created_at) VALUES (?, ?, ?, ?)",
        (pending_run_id, "run_goal", payload, created_at),
    )
    control_conn.commit()
    return {
        "status": "queued",
        "pending_run_id": pending_run_id,
        "message": "Goal queued. Run 'ledgerhand run --goal \"...\"' to execute.",
    }


class CommandReq(BaseModel):
    run_id: str
    kind: str

@app.post("/command")
def issue_command(req: CommandReq):
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    control_conn.execute(
        "INSERT INTO commands (run_id, kind, payload_json, created_at) VALUES (?, ?, ?, ?)",
        (req.run_id, req.kind, "{}", created_at)
    )
    return {"status": "ok"}

@app.post("/approve/{approval_id}")
def approve_action(approval_id: int, request: Request):
    # we need run_id
    conn = get_ledger_conn()
    if not conn: return {"status": "error"}
    cur = conn.execute("SELECT run_id FROM approvals WHERE id = ?", (approval_id,))
    row = cur.fetchone()
    if not row: return {"status": "not found"}
    
    run_id = row['run_id']
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload = json.dumps({"approval_id": approval_id})
    control_conn.execute(
        "INSERT INTO commands (run_id, kind, payload_json, created_at) VALUES (?, ?, ?, ?)",
        (run_id, "approve", payload, created_at)
    )
    return {"status": "ok"}

@app.post("/reject/{approval_id}")
def reject_action(approval_id: int, request: Request):
    conn = get_ledger_conn()
    if not conn: return {"status": "error"}
    cur = conn.execute("SELECT run_id FROM approvals WHERE id = ?", (approval_id,))
    row = cur.fetchone()
    if not row: return {"status": "not found"}
    
    run_id = row['run_id']
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload = json.dumps({"approval_id": approval_id})
    control_conn.execute(
        "INSERT INTO commands (run_id, kind, payload_json, created_at) VALUES (?, ?, ?, ?)",
        (run_id, "reject", payload, created_at)
    )
    return {"status": "ok"}

@app.get("/status")
def get_status():
    conn = get_ledger_conn()
    if not conn:
        return {"run_status": "NONE", "tasks": [], "approvals": [], "events": []}
        
    cur = conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1")
    run = cur.fetchone()
    if not run:
        return {"run_status": "NONE", "tasks": [], "approvals": [], "events": []}
        
    run_id = run['id']
    status = run['status']
    
    # check control.db for pause/stop
    ccur = control_conn.execute("SELECT kind FROM commands WHERE run_id = ? ORDER BY id DESC LIMIT 1", (run_id,))
    last_cmd = ccur.fetchone()
    if last_cmd:
        if last_cmd[0] == 'pause': status = 'PAUSED'
        elif last_cmd[0] == 'stop': status = 'STOPPED'
        elif last_cmd[0] == 'resume': status = 'RUNNING'
        
    if run['verdict']:
        status = 'FINISHED'

    # tasks
    cur = conn.execute("SELECT id, file_path, status, reason_code, decision_json FROM tasks WHERE run_id = ?", (run_id,))
    tasks = []
    for r in cur.fetchall():
        decision = json.loads(r['decision_json']) if r['decision_json'] else {}
        tasks.append({
            "id": r['id'],
            "file": r['file_path'],
            "status": r['status'],
            "reason_code": r['reason_code'],
            "bill_decision": decision.get("bill_decision", ""),
            "pay_decision": decision.get("pay_decision", "")
        })
        
    # approvals
    cur = conn.execute("SELECT id, task_id, payload_json, status FROM approvals WHERE run_id = ? AND status = 'pending'", (run_id,))
    approvals = []
    for r in cur.fetchall():
        approvals.append({
            "id": r['id'],
            "task_id": r['task_id'],
            "payload": json.loads(r['payload_json']) if r['payload_json'] else {},
            "status": r['status']
        })
        
    # events
    cur = conn.execute("SELECT ts, kind, payload_json FROM events WHERE run_id = ? ORDER BY seq DESC LIMIT 50", (run_id,))
    events = []
    for r in cur.fetchall():
        events.append({
            "ts": r['ts'],
            "kind": r['kind'],
            "payload": json.loads(r['payload_json']) if r['payload_json'] else {}
        })

    return {
        "run_id": run_id,
        "run_status": status,
        "tasks": tasks,
        "approvals": approvals,
        "events": events
    }

@app.get("/screenshot/{run_id}")
def get_screenshot(run_id: str):
    conn = get_ledger_conn()
    if not conn: return JSONResponse({"error": "No DB"}, status_code=404)
    cur = conn.execute("SELECT path FROM artifacts WHERE run_id = ? AND kind = 'screenshot' ORDER BY id DESC LIMIT 1", (run_id,))
    row = cur.fetchone()
    if row and Path(row['path']).exists():
        return FileResponse(row['path'])
    return JSONResponse({"error": "Not found"}, status_code=404)

@app.get("/runs/{run_id}/spec")
def get_spec(run_id: str):
    conn = get_ledger_conn()
    if not conn: return JSONResponse({"error": "No DB"}, status_code=404)
    cur = conn.execute("SELECT spec_json FROM runs WHERE id = ?", (run_id,))
    row = cur.fetchone()
    if row:
        return JSONResponse(json.loads(row['spec_json']))
    return JSONResponse({"error": "Not found"}, status_code=404)

class SpecUpdateReq(BaseModel):
    spec_json: str

@app.post("/runs/{run_id}/spec")
def update_spec(run_id: str, req: SpecUpdateReq):
    # Should only be allowed if AWAITING_SPEC, but the DB is read-only for dashboard!
    # The prompt says: "dashboard ONLY writes to control.db (never ledger.db)."
    # So we write a command.
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    control_conn.execute(
        "INSERT INTO commands (run_id, kind, payload_json, created_at) VALUES (?, ?, ?, ?)",
        (run_id, "confirm_spec", req.spec_json, created_at)
    )
    return {"status": "ok"}

@app.get("/presets")
def list_presets():
    return PRESETS

@app.post("/presets/{name}")
def load_preset(name: str):
    if name in PRESETS:
        return PRESETS[name]
    return JSONResponse({"error": "Not found"}, status_code=404)