import sqlite3
import json
import hashlib
from pathlib import Path
from typing import Optional, List
from datetime import datetime, timezone
from dataclasses import dataclass
from .parse import canonical_json

class ParamConflictError(Exception):
    pass

@dataclass
class Run:
    id: str
    goal_text: str
    spec_json: str
    inbox: str
    llm_mode: str
    status: str
    verdict: str
    last_command_id: int
    started_at: str
    ended_at: str

@dataclass
class Task:
    id: str
    run_id: str
    file_path: str
    file_sha256: str
    business_key: str
    status: str
    reason_code: str
    invoice_json: str
    checks_json: str
    decision_json: str
    updated_at: str

@dataclass
class Intent:
    kind: str
    business_key: str
    operation_key: str
    params_hash: str
    marker: str
    params_json: str
    expected_json: str
    status: str
    attempts: int
    prepare_failures: int
    how_confirmed: str
    run_id: str
    created_at: str
    updated_at: str

@dataclass
class Approval:
    id: int
    run_id: str
    task_id: str
    operation_key: str
    action_hash: str
    payload_json: str
    status: str
    requested_at: str
    expires_at: str
    decided_at: str
    note: str

class Ledger:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, isolation_level=None) # autocommit
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._create_tables()

    def _create_tables(self):
        self.conn.execute('''
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, goal_text TEXT, spec_json TEXT, inbox TEXT, llm_mode TEXT,
          status TEXT, verdict TEXT, last_command_id INTEGER DEFAULT 0, started_at TEXT, ended_at TEXT)
        ''')
        self.conn.execute('''
        CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, run_id TEXT, file_path TEXT, file_sha256 TEXT, business_key TEXT,
          status TEXT, reason_code TEXT, invoice_json TEXT, checks_json TEXT, decision_json TEXT, updated_at TEXT)
        ''')
        self.conn.execute('''
        CREATE TABLE IF NOT EXISTS intents(
          kind TEXT, business_key TEXT,
          operation_key TEXT, params_hash TEXT, marker TEXT UNIQUE,
          params_json TEXT, expected_json TEXT,
          status TEXT,
          attempts INTEGER DEFAULT 0, prepare_failures INTEGER DEFAULT 0,
          how_confirmed TEXT,
          run_id TEXT, created_at TEXT, updated_at TEXT,
          PRIMARY KEY(kind, business_key))
        ''')
        self.conn.execute('''
        CREATE TABLE IF NOT EXISTS approvals(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, task_id TEXT, operation_key TEXT,
          action_hash TEXT, payload_json TEXT, status TEXT DEFAULT 'pending',
          requested_at TEXT, expires_at TEXT, decided_at TEXT, note TEXT)
        ''')
        self.conn.execute('''
        CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts TEXT, kind TEXT, task_id TEXT,
          payload_json TEXT, prev_hash TEXT, hash TEXT)
        ''')
        self.conn.execute('''
        CREATE TABLE IF NOT EXISTS artifacts(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, task_id TEXT, kind TEXT, path TEXT, sha256 TEXT)
        ''')
        self.conn.execute('''
        CREATE TABLE IF NOT EXISTS extraction_cache(file_sha256 TEXT, prompt_version TEXT, invoice_json TEXT, model TEXT,
          PRIMARY KEY(file_sha256, prompt_version))
        ''')

    def create_run(self, goal_text: str, spec_json: str, inbox: str, llm_mode: str) -> str:
        run_id = "run_" + hashlib.sha256(f"{goal_text}{datetime.now(timezone.utc).isoformat()}".encode()).hexdigest()[:12]
        started_at = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO runs (id, goal_text, spec_json, inbox, llm_mode, status, started_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, goal_text, spec_json, inbox, llm_mode, "running", started_at)
        )
        return run_id

    def get_or_create_intent(self, kind: str, business_key: str, params: dict, marker: str, run_id: str) -> Intent:
        from .parse import hash_params, operation_key
        params_hash = hash_params(params)
        op_key = operation_key(kind, business_key, params_hash)
        
        cur = self.conn.execute("SELECT * FROM intents WHERE kind = ? AND business_key = ?", (kind, business_key))
        row = cur.fetchone()
        
        if row:
            if row[3] != params_hash:
                raise ParamConflictError("Different params for existing intent")
            return Intent(*row)
            
        params_json = canonical_json(params)
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO intents (kind, business_key, operation_key, params_hash, marker, params_json, status, run_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, business_key, op_key, params_hash, marker, params_json, "pending", run_id, now, now)
        )
        cur = self.conn.execute("SELECT * FROM intents WHERE kind = ? AND business_key = ?", (kind, business_key))
        return Intent(*cur.fetchone())

    def mark_dispatched(self, intent: Intent):
        self.conn.execute("UPDATE intents SET status = 'dispatched', updated_at = ? WHERE kind = ? AND business_key = ?",
                          (datetime.now(timezone.utc).isoformat(), intent.kind, intent.business_key))
        intent.status = "dispatched"

    def mark_unknown(self, intent: Intent):
        self.conn.execute("UPDATE intents SET status = 'unknown', updated_at = ? WHERE kind = ? AND business_key = ?",
                          (datetime.now(timezone.utc).isoformat(), intent.kind, intent.business_key))
        intent.status = "unknown"

    def confirm_intent(self, intent: Intent, how: str):
        self.conn.execute("UPDATE intents SET status = 'confirmed', how_confirmed = ?, updated_at = ? WHERE kind = ? AND business_key = ?",
                          (how, datetime.now(timezone.utc).isoformat(), intent.kind, intent.business_key))
        intent.status = "confirmed"
        intent.how_confirmed = how

    def log_event(self, run_id: str, kind: str, task_id: str, payload: dict) -> int:
        ts = datetime.now(timezone.utc).isoformat()
        payload_json = canonical_json(payload)
        prev_hash = self.get_last_event_hash(run_id)
        
        event_dict = {"run_id": run_id, "ts": ts, "kind": kind, "task_id": task_id, "payload": payload}
        event_json = canonical_json(event_dict)
        h = hashlib.sha256((prev_hash + event_json).encode()).hexdigest()
        
        cur = self.conn.execute("INSERT INTO events (run_id, ts, kind, task_id, payload_json, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (run_id, ts, kind, task_id, payload_json, prev_hash, h))
        return cur.lastrowid

    def get_last_event_hash(self, run_id: str) -> str:
        cur = self.conn.execute("SELECT hash FROM events WHERE run_id = ? ORDER BY seq DESC LIMIT 1", (run_id,))
        row = cur.fetchone()
        return row[0] if row else "0" * 64

    def verify_hash_chain(self, run_id: str) -> bool:
        cur = self.conn.execute("SELECT run_id, ts, kind, task_id, payload_json, prev_hash, hash FROM events WHERE run_id = ? ORDER BY seq ASC", (run_id,))
        expected_prev = "0" * 64
        for row in cur.fetchall():
            run_id, ts, kind, task_id, payload_json, prev_hash, h = row
            if prev_hash != expected_prev:
                return False
            payload = json.loads(payload_json)
            event_dict = {"run_id": run_id, "ts": ts, "kind": kind, "task_id": task_id, "payload": payload}
            event_json = canonical_json(event_dict)
            calc_h = hashlib.sha256((prev_hash + event_json).encode()).hexdigest()
            if h != calc_h:
                return False
            expected_prev = h
        return True

    def update_task_status(self, task_id: str, status: str, reason_code: str):
        self.conn.execute("UPDATE tasks SET status = ?, reason_code = ?, updated_at = ? WHERE id = ?",
                          (status, reason_code, datetime.now(timezone.utc).isoformat(), task_id))

    def get_run(self, run_id: str) -> Optional[Run]:
        cur = self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
        row = cur.fetchone()
        return Run(*row) if row else None

    def get_tasks(self, run_id: str) -> List[Task]:
        cur = self.conn.execute("SELECT * FROM tasks WHERE run_id = ?", (run_id,))
        return [Task(*row) for row in cur.fetchall()]

    def get_intents(self, run_id: str) -> List[Intent]:
        cur = self.conn.execute("SELECT * FROM intents WHERE run_id = ?", (run_id,))
        return [Intent(*row) for row in cur.fetchall()]

    def get_pending_approvals(self, run_id: str) -> List[Approval]:
        cur = self.conn.execute("SELECT * FROM approvals WHERE run_id = ? AND status = 'pending'", (run_id,))
        return [Approval(*row) for row in cur.fetchall()]

    def cache_extraction(self, file_sha256: str, prompt_version: str, invoice_json: str, model: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO extraction_cache (file_sha256, prompt_version, invoice_json, model) VALUES (?, ?, ?, ?)",
            (file_sha256, prompt_version, invoice_json, model)
        )

    def get_cached_extraction(self, file_sha256: str, prompt_version: str) -> Optional[str]:
        cur = self.conn.execute("SELECT invoice_json FROM extraction_cache WHERE file_sha256 = ? AND prompt_version = ?",
                                (file_sha256, prompt_version))
        row = cur.fetchone()
        return row[0] if row else None