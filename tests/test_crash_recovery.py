"""Crash-recovery integration tests.

Verify that LH_CRASH_AT=<hook>:<n> causes os._exit(137) at the right
moment, and that re-running the same script produces exactly 1 bill in
the ERP (no double-submit).

Run these with a live ERP:
    pytest tests/test_crash_recovery.py -m integration -v

Why a subprocess approach: crash hooks call os._exit which cannot be
tested in-process. We run the operator in a child process, verify it
crashes, then re-run without the crash flag to confirm recovery.
"""
import os
import sys
import time
import json
import subprocess
import pytest
import httpx
from pathlib import Path

# Use port 8098 (test_chaos.py uses 8099) to avoid port conflict when
# both test modules are collected in the same pytest session.
ERP_PORT = 8098

_OPERATOR_SCRIPT = """\
import sys, os
sys.path.insert(0, 'src')
os.environ.setdefault('LH_TEST', '1')
os.environ.setdefault('LLM_MODE', 'replay')

import ledgerhand.config as cfg_mod
cfg_mod.CONFIG = cfg_mod.load_config()

from ledgerhand.config import load_config
from ledgerhand.ledger import Ledger
from ledgerhand.browser.driver import Driver
from ledgerhand.models import GoalSpec, InvoiceDraft
from ledgerhand.mutation import execute_mutation
from ledgerhand.browser.skills import ensure_session
from ledgerhand.parse import business_key, normalize_invoice_no
from pathlib import Path

config = load_config()
# Use a FIXED db path (not per-run) so intent survives crash → re-run
db_path = Path({db_path!r}) / 'ledger.db'
ledger = Ledger(db_path)
driver = Driver(headless=True, allowed_paths=list(config.allowed_paths))
spec = GoalSpec()

ensure_session(driver, config)

invoice_no = {invoice_no!r}
bk = business_key('bill', 'V1', normalize_invoice_no(invoice_no))

# Use a STABLE run_id so the second run finds the same intent row
import hashlib
stable_run_id = 'crash-' + hashlib.sha256(invoice_no.encode()).hexdigest()[:12]

# get_or_create so both runs share the same run row
from ledgerhand.ledger import Ledger as _Ledger
import sqlite3, json, datetime
run = ledger.get_run(stable_run_id)
if run is None:
    ledger.conn.execute(
        "INSERT OR IGNORE INTO runs (id, goal_text, spec_json, inbox, llm_mode, status, started_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (stable_run_id, 'crash-test', '', '', 'replay', 'running',
         datetime.datetime.now(datetime.timezone.utc).isoformat())
    )
    ledger.conn.commit()

draft = InvoiceDraft(
    vendor_gstin_text='27AABCS1234A1Z5',
    vendor_gstin_confidence=0.98,
    vendor_name_text='Sharma Traders',
    invoice_no_text=invoice_no,
    invoice_no_confidence=0.98,
    invoice_date_text='01/10/2026',
    invoice_date_confidence=0.95,
    po_no_text='PO-2026-001',
    po_no_confidence=0.95,
    total_text='Rs.18,450.00',
    total_confidence=0.98,
    subtotal_text='Rs.15,635.59',
    tax_text='Rs.2,814.41',
    currency_text='INR',
)
params = {{
    'vendor_id': 'V1',
    'invoice_no': invoice_no,
    'invoice_date': '2026-10-01',
    'po_no': 'PO-2026-001',
    'currency': 'INR',
    'draft': draft,
    'amount_paise': 1_845_000,
    'vendor_account_id': 'VA1',
}}

result = execute_mutation(
    kind='create_bill',
    business_key=bk,
    params=params,
    driver=driver,
    ledger=ledger,
    spec=spec,
    config=config,
    run_id=stable_run_id,
    task_id='t-crash',
)

driver.close()
print('RESULT:', result)
"""


@pytest.fixture(scope="module")
def erp_server():
    """Start the mock ERP for crash recovery tests."""
    env = os.environ.copy()
    env.update({
        "LH_TEST": "1",
        "LB_ENFORCE_UNIQUE": "0",
        "LB_UI_VARIANT": "v1",
        "LB_USERNAME": "admin",
        "LB_PASSWORD": "ledger123",
    })
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn",
         "mock_apps.ledgerbooks.app:app",
         "--port", str(ERP_PORT),
         "--no-access-log"],
        env=env,
        cwd=str(Path(__file__).parent.parent),
    )
    url = f"http://localhost:{ERP_PORT}/login"
    for _ in range(20):
        try:
            httpx.get(url, timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    else:
        proc.terminate()
        pytest.skip("ERP server failed to start")

    yield proc
    proc.terminate()
    proc.wait(timeout=10)


@pytest.mark.integration
def test_crash_at_after_dispatch_recovers(erp_server, tmp_path):
    """G1: crash after mark_dispatched, before commit click.
    
    First run exits 137. Second run finds intent=dispatched, does
    lookup-first (bill absent), commits again. Result: 1 bill in ERP.
    """
    invoice_no = "INV-CRASH-DISPATCH"
    script_text = _OPERATOR_SCRIPT.format(
        db_path=str(tmp_path),
        invoice_no=invoice_no,
    )
    script = tmp_path / "run.py"
    script.write_text(script_text, encoding="utf-8")

    env = os.environ.copy()
    env.update({
        "LH_TEST": "1",
        "LH_CRASH_AT": "after_dispatch:1",
        "LB_BASE_URL": f"http://localhost:{ERP_PORT}",
        "LB_ENFORCE_UNIQUE": "0",
        "LLM_MODE": "replay",
        "PYTHONPATH": str(Path(__file__).parent.parent / "src"),
    })

    # Run 1 — should crash (exit code 137)
    r1 = subprocess.run([sys.executable, str(script)], env=env, timeout=60)
    assert r1.returncode != 0, "First run should have crashed"

    # Run 2 — should succeed, no duplicate
    env["LH_CRASH_AT"] = ""
    r2 = subprocess.run(
        [sys.executable, str(script)], env=env, timeout=60, capture_output=True, text=True
    )
    assert r2.returncode == 0, f"Second run failed:\n{r2.stderr}"

    # Verify exactly 1 bill in ERP
    resp = httpx.get(f"http://localhost:{ERP_PORT}/admin/state.json", timeout=5)
    bills = resp.json().get("bills", [])
    matching = [b for b in bills if b.get("invoice_no") == invoice_no]
    assert len(matching) == 1, f"Expected 1 bill, got {len(matching)}: {matching}"


@pytest.mark.integration
def test_crash_at_after_commit_recovers(erp_server, tmp_path):
    """G1: crash after commit click, before verify.
    
    Intent goes UNKNOWN. Second run: lookup finds the bill (ERP committed
    it). Intent confirmed as recovered_no_retry. Result: 1 bill in ERP.
    """
    invoice_no = "INV-CRASH-COMMIT"
    script_text = _OPERATOR_SCRIPT.format(
        db_path=str(tmp_path),
        invoice_no=invoice_no,
    )
    script = tmp_path / "run2.py"
    script.write_text(script_text, encoding="utf-8")

    env = os.environ.copy()
    env.update({
        "LH_TEST": "1",
        "LH_CRASH_AT": "after_commit:1",
        "LB_BASE_URL": f"http://localhost:{ERP_PORT}",
        "LB_ENFORCE_UNIQUE": "0",
        "LLM_MODE": "replay",
        "PYTHONPATH": str(Path(__file__).parent.parent / "src"),
    })

    # Run 1 — crash after commit
    r1 = subprocess.run([sys.executable, str(script)], env=env, timeout=60)
    assert r1.returncode != 0, "First run should have crashed"

    # Run 2 — finds bill via verify, confirms as recovered_no_retry
    env["LH_CRASH_AT"] = ""
    r2 = subprocess.run(
        [sys.executable, str(script)], env=env, timeout=60, capture_output=True, text=True
    )
    assert r2.returncode == 0, f"Second run failed:\n{r2.stderr}"

    # Verify exactly 1 bill
    resp = httpx.get(f"http://localhost:{ERP_PORT}/admin/state.json", timeout=5)
    bills = resp.json().get("bills", [])
    matching = [b for b in bills if b.get("invoice_no") == invoice_no]
    assert len(matching) == 1, f"Expected 1 bill, got {len(matching)}"
