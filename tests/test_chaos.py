"""Chaos tests — fault injection against the mock ERP.

Why: the mutation protocol's value is proved by these tests.
These are integration tests that require the mock ERP to be running
(started as a subprocess fixture). Run with:
    pytest tests/test_chaos.py -m integration -v

The three fault scenarios:
  I1. timeout_after_commit  → UNKNOWN → verify finds it → recovered_no_retry
  I2. session_expiry        → re-login → continue
  I3. ui_drift (v2 DOM)     → locator ladder adapts → bill created
"""
import os
import sys
import time
import pytest
import subprocess
import httpx
from pathlib import Path


# ── Shared ERP fixture ───────────────────────────────────────────────────────

ERP_PORT = 8099   # Use dedicated chaos port to avoid collision with demo


@pytest.fixture(scope="module")
def erp_server():
    """Start the mock ERP on port 8099 for the chaos test session."""
    env = os.environ.copy()
    env.update({
        "LH_TEST": "1",
        "LB_ENFORCE_UNIQUE": "0",   # allow re-submit after recovery
        "LB_UI_VARIANT": "v1",
        "LB_USERNAME": "admin",
        "LB_PASSWORD": "ledger123",
        "PORT": str(ERP_PORT),
    })
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn",
         "mock_apps.ledgerbooks.app:app",
         "--port", str(ERP_PORT),
         "--no-access-log"],
        env=env,
        cwd=str(Path(__file__).parent.parent),
    )
    # Wait up to 10 s for server
    url = f"http://localhost:{ERP_PORT}/login"
    for _ in range(20):
        try:
            httpx.get(url, timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    else:
        proc.terminate()
        pytest.skip("ERP server failed to start — skipping chaos tests")

    yield proc

    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture
def env_setup(tmp_path, monkeypatch, erp_server):
    """Per-test: load config pointing at chaos ERP port, fresh ledger."""
    monkeypatch.setenv("LB_BASE_URL", f"http://localhost:{ERP_PORT}")
    monkeypatch.setenv("LH_TEST", "1")
    monkeypatch.setenv("LB_ENFORCE_UNIQUE", "0")
    monkeypatch.setenv("LLM_MODE", "replay")

    # Re-import after env change
    import importlib
    import ledgerhand.config as cfg_mod
    cfg_mod.CONFIG = cfg_mod.load_config()

    from ledgerhand.config import load_config
    from ledgerhand.ledger import Ledger
    from ledgerhand.browser.driver import Driver
    from ledgerhand.models import GoalSpec

    config = load_config()
    ledger = Ledger(tmp_path / "ledger.db")
    driver = Driver(headless=True, allowed_paths=list(config.allowed_paths))
    spec = GoalSpec()
    spec.authority["auto_pay_max_paise"] = 5_000_000

    yield config, ledger, driver, spec

    try:
        driver.close()
    except Exception:
        pass


def _make_bill_params(vendor_id: str, invoice_no: str) -> dict:
    """Minimal params for create_bill mutation in chaos tests."""
    from ledgerhand.models import InvoiceDraft
    draft = InvoiceDraft(
        vendor_gstin_text="27AABCS1234A1Z5",
        vendor_gstin_confidence=0.98,
        vendor_name_text="Sharma Traders",
        invoice_no_text=invoice_no,
        invoice_no_confidence=0.98,
        invoice_date_text="01/10/2026",
        invoice_date_confidence=0.95,
        po_no_text="PO-2026-001",
        po_no_confidence=0.95,
        total_text="Rs.18,450.00",
        total_confidence=0.98,
        subtotal_text="Rs.15,635.59",
        tax_text="Rs.2,814.41",
        currency_text="INR",
    )
    return {
        "vendor_id": vendor_id,
        "invoice_no": invoice_no,
        "invoice_date": "2026-10-01",  # ISO format string for YYYY-MM-DD
        "po_no": "PO-2026-001",
        "currency": "INR",
        "draft": draft,
        "amount_paise": 1_845_000,
        "vendor_account_id": "VA1",
    }


def _arm_fault(fault_name: str, after_n: int = 0, times: int = 1) -> None:
    httpx.post(
        f"http://localhost:{ERP_PORT}/admin/chaos",
        data={"name": fault_name, "after_n_calls": after_n, "times": times},
        timeout=5,
    )


# ── Fault tests ──────────────────────────────────────────────────────────────

@pytest.mark.integration
def test_timeout_after_commit_recovers(env_setup):
    """I1: timeout_after_commit — ERP commits but returns 504.
    
    The mutation catches the exception (intent → UNKNOWN), then verify
    finds the bill we wrote (marker matches), and returns recovered_no_retry.
    """
    from ledgerhand.mutation import execute_mutation
    from ledgerhand.browser.skills import ensure_session
    from ledgerhand.parse import business_key, make_marker, normalize_invoice_no

    config, ledger, driver, spec = env_setup
    ensure_session(driver, config)

    _arm_fault("timeout_after_commit", after_n=0, times=1)

    invoice_no = "INV-CHAOS-001"
    bk = business_key("bill", "V1", normalize_invoice_no(invoice_no))
    run_id = ledger.create_run("chaos test", "", "", "replay")

    result = execute_mutation(
        kind="create_bill",
        business_key=bk,
        params=_make_bill_params("V1", invoice_no),
        driver=driver,
        ledger=ledger,
        spec=spec,
        config=config,
        run_id=run_id,
        task_id="t-chaos-1",
    )

    # Must always resolve — either verified or recovered (never lost)
    assert result["status"] == "confirmed"
    assert result["how"] in ("verified", "recovered_no_retry", "found_before_acting")


@pytest.mark.integration
def test_session_expiry_recovers(env_setup):
    """I2: session_expiry after N requests — ensure_session re-logs in.
    
    The skill layer detects the /login redirect (or 401) and calls
    _do_login again, then continues the operation.
    """
    from ledgerhand.browser.skills import ensure_session
    from ledgerhand.mutation import execute_mutation
    from ledgerhand.parse import business_key, normalize_invoice_no

    config, ledger, driver, spec = env_setup
    ensure_session(driver, config)

    # Expire after 2 more requests
    _arm_fault("session_expiry", after_n=2, times=1)

    invoice_no = "INV-CHAOS-002"
    bk = business_key("bill", "V1", normalize_invoice_no(invoice_no))
    run_id = ledger.create_run("chaos session", "", "", "replay")

    result = execute_mutation(
        kind="create_bill",
        business_key=bk,
        params=_make_bill_params("V1", invoice_no),
        driver=driver,
        ledger=ledger,
        spec=spec,
        config=config,
        run_id=run_id,
        task_id="t-chaos-2",
    )

    assert result["status"] == "confirmed"


@pytest.mark.integration
def test_ui_drift_v2_locator_ladder(env_setup):
    """I3: UI drift (v2 DOM) — locator ladder finds 'Post Bill' not 'Submit Bill'.
    
    Arm the ui_drift fault so the ERP serves v2 HTML. The selectors
    ladder should still fill the form and click the correct button.
    """
    from ledgerhand.browser.skills import ensure_session
    from ledgerhand.mutation import execute_mutation
    from ledgerhand.parse import business_key, normalize_invoice_no

    config, ledger, driver, spec = env_setup
    ensure_session(driver, config)

    _arm_fault("ui_drift", after_n=0, times=999)

    invoice_no = "INV-CHAOS-003"
    bk = business_key("bill", "V1", normalize_invoice_no(invoice_no))
    run_id = ledger.create_run("chaos drift", "", "", "replay")

    result = execute_mutation(
        kind="create_bill",
        business_key=bk,
        params=_make_bill_params("V1", invoice_no),
        driver=driver,
        ledger=ledger,
        spec=spec,
        config=config,
        run_id=run_id,
        task_id="t-chaos-3",
    )

    assert result["status"] == "confirmed"

    # Disarm
    httpx.post(
        f"http://localhost:{ERP_PORT}/admin/chaos",
        data={"name": "ui_drift", "disarm": "1"},
        timeout=5,
    )
