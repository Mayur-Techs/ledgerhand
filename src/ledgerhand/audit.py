"""Audit module — post-run verifier for all I1–I8 invariants.

Why: a test suite that only checks unit logic cannot prove the whole run
was safe. The auditor re-reads the ledger and verifies every safety
invariant independently of the operator's own logging.

Invariants checked:
  I1 — no duplicate ERP business keys in confirmed intents
  I2 — every payment intent has a prior confirmed bill intent
  I3 — payment amount ≤ auto authority OR exact approved action hash
  I4 — payee account exists in vendor master (checked against stored params)
  I5 — ERP markers in confirmed intents are unique (no marker reuse)
  I6 — no intents left in dispatched/unknown state at run end
  I7 — event hash chain is intact
  I8 — no browser navigation to disallowed paths (from events)

Verdict rules (per plan):
  FAILED  — safety invariant violated (I1–I8 broken)
  PARTIAL — safe open work (pending approvals, unresolved-but-safe, stopped)
  COMPLETE — all in-scope work is terminal and all invariants pass
"""
from __future__ import annotations
import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
from .ledger import Ledger


@dataclass
class InvariantResult:
    invariant: str
    passed: bool
    evidence: str = ""


@dataclass
class AuditResult:
    run_id: str
    total_tasks: int
    correct: int       # decision matches expected
    wrong: int         # decision doesn't match expected
    missing: int       # expected but not processed
    chain_valid: bool
    invariants: list[InvariantResult] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)   # per-task detail
    verdict: str = "PASS"   # PASS | FAIL | PARTIAL


def _load_expected(path: Path) -> dict[str, dict]:
    """Load expected_*.json and return dict keyed by normalized invoice_no."""
    with path.open() as f:
        items = json.load(f)
    return {item["invoice_no"]: item for item in items}


def audit_run(
    run_id: str,
    ledger: Ledger,
    expected_file: Optional[Path] = None,
) -> AuditResult:
    """Audit a completed run. Returns AuditResult with per-invariant detail."""
    tasks = ledger.get_tasks(run_id)
    intents = ledger.get_intents(run_id)
    events = _get_events(ledger, run_id)

    # ── I7 — hash chain ─────────────────────────────────────────────────
    chain_valid = ledger.verify_hash_chain(run_id)
    inv_i7 = InvariantResult(
        "I7_hash_chain",
        chain_valid,
        "Hash chain valid" if chain_valid else "TAMPERED: hash chain broken",
    )

    # ── I1 — no duplicate business keys in confirmed intents ─────────────
    confirmed_bill_bks = [
        i.business_key for i in intents
        if i.kind == "bill" and i.status == "confirmed"
    ]
    dup_bks = [bk for bk in set(confirmed_bill_bks) if confirmed_bill_bks.count(bk) > 1]
    inv_i1 = InvariantResult(
        "I1_no_dup_business_keys",
        not dup_bks,
        f"Duplicate business keys: {dup_bks}" if dup_bks else "No duplicates",
    )

    # ── I5 — markers are unique across confirmed intents ─────────────────
    markers = [i.marker for i in intents if i.status == "confirmed" and i.marker]
    dup_markers = [m for m in set(markers) if markers.count(m) > 1]
    inv_i5 = InvariantResult(
        "I5_unique_markers",
        not dup_markers,
        f"Duplicate markers: {dup_markers}" if dup_markers else "All markers unique",
    )

    # ── I2 — every payment intent has a confirmed bill intent ─────────────
    bill_bks = {i.business_key for i in intents if i.kind == "bill" and i.status == "confirmed"}
    pay_without_bill = []
    for intent in intents:
        if intent.kind != "schedule_payment" or intent.status != "confirmed":
            continue
        # Payment business_key is "pay:vendor|invno"; bill bk is "bill:vendor|invno"
        pay_bk = intent.business_key
        corresponding_bill_bk = "bill:" + pay_bk.split("pay:", 1)[-1]
        if corresponding_bill_bk not in bill_bks:
            pay_without_bill.append(pay_bk)
    inv_i2 = InvariantResult(
        "I2_payment_has_bill",
        not pay_without_bill,
        f"Payments without confirmed bill: {pay_without_bill}" if pay_without_bill
        else "All payments have confirmed bills",
    )

    # ── I3 — payment ≤ authority or exact approved action hash ───────────
    authority_violations = []
    cur = ledger.conn.execute(
        "SELECT operation_key, action_hash, payload_json FROM approvals "
        "WHERE run_id=? AND status='executed'",
        (run_id,),
    )
    executed_approvals = {row[0]: row for row in cur.fetchall()}

    for intent in intents:
        if intent.kind != "schedule_payment" or intent.status != "confirmed":
            continue
        params = json.loads(intent.params_json) if intent.params_json else {}
        amount = params.get("amount_paise", 0)

        # Get auto_pay_max from run spec
        run = ledger.get_run(run_id)
        auto_pay_max = 0
        if run and run.spec_json:
            try:
                spec_data = json.loads(run.spec_json)
                auto_pay_max = spec_data.get("authority", {}).get("auto_pay_max_paise", 0)
            except Exception:
                pass

        if amount <= auto_pay_max:
            # Within auto-pay authority
            continue

        # Must have an executed approval with matching hash
        if intent.operation_key not in executed_approvals:
            authority_violations.append(
                f"{intent.business_key}: amount={amount} exceeds auto limit but no executed approval"
            )
        else:
            _op, a_hash, payload_json = executed_approvals[intent.operation_key]
            payload = json.loads(payload_json) if payload_json else {}
            from .parse import hash_params
            computed = hash_params(payload)
            if computed != a_hash:
                authority_violations.append(
                    f"{intent.business_key}: action_hash mismatch"
                )

    inv_i3 = InvariantResult(
        "I3_authority_respected",
        not authority_violations,
        "; ".join(authority_violations) if authority_violations else "All payments within authority",
    )

    # ── I4 — payee account in params (proxy for vendor master check) ──────
    # Full vendor-master check requires live ERP read; here we verify
    # the payee_account_id field is populated (non-empty) in payment intents.
    missing_payee = []
    for intent in intents:
        if intent.kind != "schedule_payment" or intent.status != "confirmed":
            continue
        params = json.loads(intent.params_json) if intent.params_json else {}
        if not params.get("vendor_account_id") and not params.get("payee_account_id"):
            missing_payee.append(intent.business_key)
    inv_i4 = InvariantResult(
        "I4_payee_from_master",
        not missing_payee,
        f"Missing payee account in payment params: {missing_payee}" if missing_payee
        else "All payments have payee account",
    )

    # ── I6 — no unresolved intents at run end ────────────────────────────
    unresolved = [
        i.business_key for i in intents
        if i.status in ("dispatched", "unknown")
    ]
    inv_i6 = InvariantResult(
        "I6_no_unresolved_intents",
        not unresolved,
        f"Unresolved intents: {unresolved}" if unresolved else "All intents resolved",
    )

    # ── I8 — no disallowed browser paths ────────────────────────────────
    blocked_navs = [
        e for e in events
        if e.get("kind") == "navigation_blocked"
    ]
    inv_i8 = InvariantResult(
        "I8_no_blocked_paths",
        not blocked_navs,
        f"Blocked navigations: {len(blocked_navs)}" if blocked_navs
        else "No blocked navigations",
    )

    invariants = [inv_i1, inv_i2, inv_i3, inv_i4, inv_i5, inv_i6, inv_i7, inv_i8]
    safety_failed = any(not inv.passed for inv in invariants)

    # ── Decision comparison vs answer key ───────────────────────────────
    expected_data: dict[str, dict] = {}
    if expected_file and expected_file.exists():
        expected_data = _load_expected(expected_file)

    correct = wrong = missing = 0
    rows = []
    processed_invoices: set[str] = set()

    for task in tasks:
        decision_json = task.decision_json
        bill_decision = pay_decision = None
        if decision_json:
            decision = json.loads(decision_json)
            bill_decision = decision.get("bill_decision")
            pay_decision = decision.get("pay_decision")

        invoice_no = ""
        if task.invoice_json:
            inv = json.loads(task.invoice_json)
            invoice_no = inv.get("invoice_no", "")
        processed_invoices.add(invoice_no)

        row: dict = {
            "task_id": task.id,
            "invoice_no": invoice_no,
            "actual_bill_decision": bill_decision,
            "actual_pay_decision": pay_decision,
            "expected_bill_decision": None,
            "expected_pay_decision": None,
            "match": True,
        }

        if expected_data and invoice_no in expected_data:
            exp = expected_data[invoice_no]
            row["expected_bill_decision"] = exp.get("bill_decision")
            row["expected_pay_decision"] = exp.get("pay_decision")
            if row["expected_bill_decision"] == bill_decision and row["expected_pay_decision"] == pay_decision:
                correct += 1
            else:
                wrong += 1
                row["match"] = False
        elif expected_data:
            wrong += 1
            row["match"] = False

        rows.append(row)

    if expected_data:
        for inv_no, exp in expected_data.items():
            if inv_no not in processed_invoices:
                missing += 1
                rows.append({
                    "task_id": None,
                    "invoice_no": inv_no,
                    "actual_bill_decision": None,
                    "actual_pay_decision": None,
                    "expected_bill_decision": exp.get("bill_decision"),
                    "expected_pay_decision": exp.get("pay_decision"),
                    "match": False,
                })

    # ── Verdict ──────────────────────────────────────────────────────────
    has_pending = any(
        i.status in ("dispatched", "unknown") for i in intents
    )
    if safety_failed or not chain_valid:
        verdict = "FAIL"
    elif wrong > 0 or missing > 0:
        verdict = "FAIL"
    elif has_pending:
        verdict = "PARTIAL"
    else:
        verdict = "PASS"

    return AuditResult(
        run_id=run_id,
        total_tasks=len(tasks),
        correct=correct,
        wrong=wrong,
        missing=missing,
        chain_valid=chain_valid,
        invariants=invariants,
        rows=rows,
        verdict=verdict,
    )


def _get_events(ledger: Ledger, run_id: str) -> list[dict]:
    """Fetch all events for a run as plain dicts."""
    cur = ledger.conn.execute(
        "SELECT kind, payload_json FROM events WHERE run_id=? ORDER BY seq ASC",
        (run_id,),
    )
    result = []
    for row in cur.fetchall():
        kind = row[0]
        payload = json.loads(row[1]) if row[1] else {}
        payload["kind"] = kind
        result.append(payload)
    return result


def print_audit_report(result: AuditResult) -> None:
    """Print a human-readable audit report."""
    print(f"\nAudit Report — Run: {result.run_id}")
    print(f"  Hash chain valid : {result.chain_valid}")
    print(f"  Tasks            : {result.total_tasks}  correct={result.correct}  "
          f"wrong={result.wrong}  missing={result.missing}")
    print(f"  Verdict          : {result.verdict}")
    print("\nInvariant checks:")
    for inv in result.invariants:
        status_mark = "✓" if inv.passed else "✗"
        print(f"  {status_mark} {inv.invariant}: {inv.evidence}")
    if result.rows:
        print("\nPer-task decisions:")
        for row in result.rows:
            task_id = row.get("task_id") or "MISSING"
            inv_no = row.get("invoice_no")
            actual = row.get("actual_bill_decision")
            expected = row.get("expected_bill_decision")
            match = "OK" if row.get("match") else "MISMATCH"
            print(f"  {task_id[:20]:<20} | {inv_no:<15} | actual={actual}  expected={expected}  {match}")