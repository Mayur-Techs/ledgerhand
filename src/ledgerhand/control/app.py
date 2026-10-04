"""Dashboard — FastAPI app on port 8000.

The dashboard is the ONLY writer to control.db.
It reads ledger.db read-only (mode=ro).
Why: single-writer rule prevents SQLite lock contention.

Thread-safety: SQLite connections are created per-request (check_same_thread=False
is NOT used). Instead we open a fresh connection in every handler so uvicorn's
threadpool never shares a connection across threads.
"""
from fastapi import FastAPI, Request, Form
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
from pydantic import BaseModel
import sqlite3
import json
import time
from pathlib import Path
from typing import Optional

from ledgerhand.control.presets import PRESETS

app = FastAPI()

templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

# ── DB paths ─────────────────────────────────────────────────────────────────
runs_dir = Path("runs")
runs_dir.mkdir(exist_ok=True)
control_db_path = runs_dir / "control.db"
ledger_db_path  = runs_dir / "ledger.db"


def _get_control_conn() -> sqlite3.Connection:
    """Open a fresh control.db connection for the calling thread."""
    conn = sqlite3.connect(str(control_db_path))
    conn.row_factory = sqlite3.Row
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


def _get_ledger_conn() -> Optional[sqlite3.Connection]:
    """Open ledger.db read-only; returns None if the file doesn't exist yet."""
    if not ledger_db_path.exists():
        return None
    uri = f"file:{ledger_db_path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "request": request,
        "presets": PRESETS,
    })


@app.post("/goal")
async def submit_goal(
    request: Request,
    goal_text: str = Form(...),
    inbox: str = Form(default="data/batch_a"),
):
    """Queue a natural-language goal into control.db.

    The runner polls control.db for 'run_goal' commands and starts the job.
    Dashboard never writes to ledger.db.
    """
    import uuid
    pending_run_id = f"pending_{uuid.uuid4().hex[:8]}"
    payload = json.dumps({"goal_text": goal_text, "inbox": inbox})
    conn = _get_control_conn()
    conn.execute(
        "INSERT INTO commands (run_id, kind, payload_json, created_at) VALUES (?, ?, ?, ?)",
        (pending_run_id, "run_goal", payload, _now()),
    )
    conn.commit()
    conn.close()
    return {
        "status": "queued",
        "pending_run_id": pending_run_id,
        "message": (
            f"Goal queued as {pending_run_id}. "
            "Run `python -m ledgerhand run --goal \"...\"` in the terminal to execute."
        ),
    }


class CommandReq(BaseModel):
    run_id: str
    kind: str


@app.post("/command")
def issue_command(req: CommandReq):
    conn = _get_control_conn()
    conn.execute(
        "INSERT INTO commands (run_id, kind, payload_json, created_at) VALUES (?, ?, ?, ?)",
        (req.run_id, req.kind, "{}", _now()),
    )
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.post("/approve/{approval_id}")
def approve_action(approval_id: int):
    lconn = _get_ledger_conn()
    if not lconn:
        return JSONResponse({"status": "error", "detail": "No ledger DB"}, status_code=404)
    row = lconn.execute("SELECT run_id FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    lconn.close()
    if not row:
        return JSONResponse({"status": "not found"}, status_code=404)
    conn = _get_control_conn()
    conn.execute(
        "INSERT INTO commands (run_id, kind, payload_json, created_at) VALUES (?, ?, ?, ?)",
        (row["run_id"], "approve", json.dumps({"approval_id": approval_id}), _now()),
    )
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.post("/reject/{approval_id}")
def reject_action(approval_id: int):
    lconn = _get_ledger_conn()
    if not lconn:
        return JSONResponse({"status": "error", "detail": "No ledger DB"}, status_code=404)
    row = lconn.execute("SELECT run_id FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    lconn.close()
    if not row:
        return JSONResponse({"status": "not found"}, status_code=404)
    conn = _get_control_conn()
    conn.execute(
        "INSERT INTO commands (run_id, kind, payload_json, created_at) VALUES (?, ?, ?, ?)",
        (row["run_id"], "reject", json.dumps({"approval_id": approval_id}), _now()),
    )
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.get("/status")
def get_status():
    lconn = _get_ledger_conn()
    if not lconn:
        return {"run_status": "NONE", "run_id": None, "tasks": [], "approvals": [], "events": [], "verdict": None}

    run = lconn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    if not run:
        lconn.close()
        return {"run_status": "NONE", "run_id": None, "tasks": [], "approvals": [], "events": [], "verdict": None}

    run_id = run["id"]
    verdict = run["verdict"]
    goal_text = run["goal_text"]
    status = run["status"]

    # Check control.db for pause/stop override
    cconn = _get_control_conn()
    last_cmd = cconn.execute(
        "SELECT kind FROM commands WHERE run_id = ? ORDER BY id DESC LIMIT 1", (run_id,)
    ).fetchone()
    cconn.close()

    if last_cmd:
        k = last_cmd["kind"] if isinstance(last_cmd, sqlite3.Row) else last_cmd[0]
        if k == "pause":   status = "PAUSED"
        elif k == "stop":  status = "STOPPED"
        elif k == "resume": status = "RUNNING"

    if verdict:
        status = "FINISHED"

    # Tasks
    tasks = []
    for r in lconn.execute(
        "SELECT id, file_path, status, reason_code, decision_json FROM tasks WHERE run_id = ?",
        (run_id,)
    ).fetchall():
        decision = json.loads(r["decision_json"]) if r["decision_json"] else {}
        tasks.append({
            "id":            r["id"],
            "file":          r["file_path"],
            "status":        r["status"],
            "reason_code":   r["reason_code"] or "",
            "bill_decision": decision.get("bill_decision", ""),
            "pay_decision":  decision.get("pay_decision", ""),
        })

    # Pending approvals
    approvals = []
    for r in lconn.execute(
        "SELECT id, task_id, payload_json, status, expires_at FROM approvals WHERE run_id = ? AND status = 'pending'",
        (run_id,)
    ).fetchall():
        approvals.append({
            "id":         r["id"],
            "task_id":    r["task_id"],
            "payload":    json.loads(r["payload_json"]) if r["payload_json"] else {},
            "status":     r["status"],
            "expires_at": r["expires_at"],
        })

    # Recent events (last 100, newest first)
    events = []
    for r in lconn.execute(
        "SELECT ts, kind, task_id, payload_json FROM events WHERE run_id = ? ORDER BY seq DESC LIMIT 100",
        (run_id,)
    ).fetchall():
        events.append({
            "ts":      r["ts"],
            "kind":    r["kind"],
            "task_id": r["task_id"],
            "payload": json.loads(r["payload_json"]) if r["payload_json"] else {},
        })

    lconn.close()
    return {
        "run_id":     run_id,
        "run_status": status,
        "verdict":    verdict,
        "goal_text":  goal_text,
        "tasks":      tasks,
        "approvals":  approvals,
        "events":     events,
    }


@app.get("/screenshot/{run_id}")
def get_screenshot(run_id: str):
    lconn = _get_ledger_conn()
    if not lconn:
        return JSONResponse({"error": "No DB"}, status_code=404)
    row = lconn.execute(
        "SELECT path FROM artifacts WHERE run_id = ? AND kind = 'screenshot' ORDER BY id DESC LIMIT 1",
        (run_id,)
    ).fetchone()
    lconn.close()
    if row and Path(row["path"]).exists():
        return FileResponse(row["path"])
    return JSONResponse({"error": "Not found"}, status_code=404)


@app.get("/runs/{run_id}/spec")
def get_spec(run_id: str):
    lconn = _get_ledger_conn()
    if not lconn:
        return JSONResponse({"error": "No DB"}, status_code=404)
    row = lconn.execute("SELECT spec_json FROM runs WHERE id = ?", (run_id,)).fetchone()
    lconn.close()
    if row:
        return JSONResponse(json.loads(row["spec_json"]))
    return JSONResponse({"error": "Not found"}, status_code=404)


class SpecUpdateReq(BaseModel):
    spec_json: str


@app.post("/runs/{run_id}/spec")
def update_spec(run_id: str, req: SpecUpdateReq):
    conn = _get_control_conn()
    conn.execute(
        "INSERT INTO commands (run_id, kind, payload_json, created_at) VALUES (?, ?, ?, ?)",
        (run_id, "confirm_spec", req.spec_json, _now()),
    )
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.get("/presets")
def list_presets():
    return PRESETS


@app.post("/presets/{name}")
def load_preset(name: str):
    if name in PRESETS:
        return PRESETS[name]
    return JSONResponse({"error": "Not found"}, status_code=404)