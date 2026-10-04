"""Runner — orchestrates the full AP workflow.

Why: a ~250-line state machine is defensible line by line.
No LangGraph, no async, no decorator magic. Every decision
can be traced to a line of code.

Data flow per invoice:
  extract_pdf → parse_draft → vendor/PO lookup → run_all_checks
  → decide → execute_mutation (ENTER) → approval/payment loop

Single-writer rule: runner writes ONLY to ledger.db.
Dashboard writes ONLY to control.db.
"""
from __future__ import annotations

import json
import time
import hashlib
import sqlite3
import datetime
from pathlib import Path
from typing import Optional

from ledgerhand.ledger import Ledger
from ledgerhand.browser.driver import Driver
from ledgerhand.config import Config
from ledgerhand.models import GoalSpec, VendorRecord, PORecord
from ledgerhand.extract import extract_pdf, parse_draft
from ledgerhand.parse import (
    business_key, normalize_invoice_no, canonical_json,
    operation_key, hash_params, action_hash,
)
from ledgerhand.checks import run_all_checks
from ledgerhand.policy import decide
from ledgerhand.mutation import execute_mutation, NeedsHuman
from ledgerhand.audit import audit_run


class Runner:
    def __init__(self, ledger: Ledger, driver: Driver, config: Config, run_id: str):
        self.ledger = ledger
        self.driver = driver
        self.config = config
        self.run_id = run_id

        # Dashboard writes to control.db; we read commands from it
        control_db = Path(self.ledger.db_path).parent / "control.db"
        if control_db.exists():
            self.control_conn = sqlite3.connect(str(control_db), isolation_level=None)
            self.control_conn.row_factory = sqlite3.Row
        else:
            self.control_conn = None
        self.last_command_id = 0

    # ─────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────

    def run(self, spec: GoalSpec, inbox: Path, llm_mode: str) -> str:
        """Main loop. Returns run verdict: COMPLETE | PARTIAL | FAILED."""
        # Snapshot inbox at start — files added later wait for next run
        files = sorted(f for f in inbox.iterdir() if f.suffix == ".pdf")

        verdict = "COMPLETE"
        tasks_needing_approval: list[dict] = []

        for file_path in files:
            # Poll for operator commands at each safe point
            cmd = self._check_commands()
            if cmd == "stop":
                self._finalize_run("STOPPED", "PARTIAL")
                return "PARTIAL"
            while cmd == "pause":
                time.sleep(1)
                cmd = self._check_commands()
                if cmd == "stop":
                    self._finalize_run("STOPPED", "PARTIAL")
                    return "PARTIAL"

            result = self._process_file(file_path, spec, llm_mode, tasks_needing_approval)
            if result == "error":
                verdict = "PARTIAL"

        # Wait for and execute approved payments
        if tasks_needing_approval:
            self._wait_for_approvals(tasks_needing_approval, spec)

        # Finalize run in ledger
        audit_result = audit_run(self.run_id, self.ledger)
        final_verdict = audit_result.verdict if audit_result.verdict == "FAIL" else verdict
        self._finalize_run("FINISHED", final_verdict)
        return final_verdict

    def resume(self, spec: GoalSpec, inbox: Path, llm_mode: str) -> str:
        """Resume from a stopped/crashed run.

        Re-enters each task from its last persisted state.
        Tasks already 'done' are skipped.
        Tasks in 'dispatched'/'unknown' are re-tried via lookup-first.
        """
        tasks = self.ledger.get_tasks(self.run_id)
        done_files = {t.file_path for t in tasks if t.status == "done"}

        # Find all inbox files not yet done
        files = sorted(f for f in inbox.iterdir() if f.suffix == ".pdf")
        pending_files = [f for f in files if f.name not in done_files]

        verdict = "COMPLETE"
        tasks_needing_approval: list[dict] = []

        for file_path in pending_files:
            cmd = self._check_commands()
            if cmd == "stop":
                self._finalize_run("STOPPED", "PARTIAL")
                return "PARTIAL"

            result = self._process_file(file_path, spec, llm_mode, tasks_needing_approval)
            if result == "error":
                verdict = "PARTIAL"

        if tasks_needing_approval:
            self._wait_for_approvals(tasks_needing_approval, spec)

        audit_result = audit_run(self.run_id, self.ledger)
        final_verdict = audit_result.verdict if audit_result.verdict == "FAIL" else verdict
        self._finalize_run("FINISHED", final_verdict)
        return final_verdict

    # ─────────────────────────────────────────────────────────────────────
    # Per-file processing
    # ─────────────────────────────────────────────────────────────────────

    def _process_file(
        self,
        file_path: Path,
        spec: GoalSpec,
        llm_mode: str,
        tasks_needing_approval: list,
    ) -> str:
        """Process one invoice file. Returns 'done' or 'error'."""
        file_bytes = file_path.read_bytes()
        file_sha256 = hashlib.sha256(file_bytes).hexdigest()
        task_id = f"task_{file_sha256[:12]}"
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()

        # Create or skip (idempotent on resume)
        self.ledger.conn.execute(
            "INSERT OR IGNORE INTO tasks "
            "(id, run_id, file_path, file_sha256, status, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, self.run_id, file_path.name, file_sha256, "processing", now),
        )

        try:
            # a. Extract (cached by sha256 + prompt_version)
            _sha, draft = extract_pdf(file_path, self.config, llm_mode, self.ledger)

            # b. Parse — code does all arithmetic, never the model
            parsed, parse_issues = parse_draft(draft, self.config)
            if parsed is None:
                reason = parse_issues[0].evidence if parse_issues else "PARSE_FAILED"
                self.ledger.update_task_status(task_id, "hold", reason)
                self.ledger.log_event(self.run_id, "parse_failed", task_id, {"issues": [i.evidence for i in parse_issues]})
                return "done"

            # c. Resolve vendor (by GSTIN from vendor master via ERP read)
            vendor = self._lookup_vendor(parsed.vendor_gstin)
            if vendor:
                # Patch vendor_id into parsed (it's not frozen yet at this stage)
                parsed = parsed.model_copy(update={"vendor_id": vendor.id})

            # d. Resolve PO
            po = self._lookup_po(parsed.po_no) if parsed.po_no else None

            # e. Lookup existing bills for duplicate check
            existing_bills = self._list_existing_bills(parsed.vendor_id or "")

            # f. Build business key
            bk = business_key("bill", parsed.vendor_id or "", normalize_invoice_no(parsed.invoice_no_raw))

            # g. Run all checks
            checks = run_all_checks(parsed, vendor, po, existing_bills, self.config)
            checks_json = canonical_json([c.model_dump() for c in checks])

            # h. Check for existing intent (resume path)
            existing_intent = None
            cur = self.ledger.conn.execute(
                "SELECT * FROM intents WHERE kind='bill' AND business_key=?", (bk,)
            )
            row = cur.fetchone()
            if row:
                from ledgerhand.ledger import Intent
                existing_intent = Intent(*row)

            # i. Decide
            decision = decide(checks, parsed, spec, self.config, existing_intent)

            # j. Persist task fields
            invoice_json = canonical_json(parsed.model_dump(mode="json"))
            self.ledger.conn.execute(
                "UPDATE tasks SET business_key=?, invoice_json=?, checks_json=?, "
                "decision_json=?, reason_code=? WHERE id=?",
                (bk, invoice_json, checks_json,
                 canonical_json(decision.model_dump()), decision.reason_code, task_id),
            )

            self.ledger.log_event(self.run_id, "decision", task_id, {
                "bill": decision.bill_decision,
                "pay": decision.pay_decision,
                "reason": decision.reason_code,
            })

            # k. Act on decision
            if decision.bill_decision == "ENTER":
                # Determine vendor account from master (NOT from invoice)
                vendor_account_id = ""
                if vendor:
                    acct = vendor.default_account()
                    if acct:
                        vendor_account_id = acct.id

                bill_params = {
                    "draft": draft.model_dump(),
                    "vendor_id": parsed.vendor_id or "",
                    "invoice_no": parsed.invoice_no,
                    "invoice_date": str(parsed.invoice_date),
                    "po_no": parsed.po_no,
                    "currency": parsed.currency,
                    "amount_paise": parsed.total_paise,
                    "vendor_account_id": vendor_account_id,
                    "marker": "",  # filled by execute_mutation
                }
                try:
                    mut_result = execute_mutation(
                        "create_bill", bk, bill_params,
                        self.driver, self.ledger, spec, self.config,
                        self.run_id, task_id,
                    )
                    self.ledger.log_event(self.run_id, "bill_entered", task_id, {
                        "how": mut_result.get("how"),
                    })
                except NeedsHuman as e:
                    self.ledger.update_task_status(task_id, "needs_human", str(e))
                    self.ledger.log_event(self.run_id, "needs_human", task_id, {"reason": str(e)})
                    return "done"

                # l. Handle payment decision
                if decision.pay_decision == "AUTO":
                    self._execute_payment(bk, parsed, spec, task_id, approved=True)

                elif decision.pay_decision == "APPROVE":
                    appr_payload = self._request_approval(bk, parsed, task_id, now)
                    tasks_needing_approval.append({
                        "task_id": task_id,
                        "business_key": bk,
                        "parsed": parsed,
                        "spec": spec,
                        "payload": appr_payload,
                    })

            self.ledger.update_task_status(task_id, "done", decision.reason_code)
            return "done"

        except Exception as e:
            self.ledger.update_task_status(task_id, "error", str(e)[:200])
            self.ledger.log_event(self.run_id, "task_error", task_id, {"error": str(e)})
            return "error"

    # ─────────────────────────────────────────────────────────────────────
    # ERP data helpers (read-only; vendor/PO master via browser skills)
    # ─────────────────────────────────────────────────────────────────────

    def _lookup_vendor(self, gstin: str) -> Optional[VendorRecord]:
        """Read vendor master from ERP by GSTIN."""
        try:
            from ledgerhand.browser.skills import find_vendor
            return find_vendor(self.driver, self.config, gstin)
        except Exception as e:
            print(f"[debug] Exception in _lookup_vendor: {e}")
            import traceback
            traceback.print_exc()
        return None

    def _lookup_po(self, po_no: str) -> Optional[PORecord]:
        """Read PO from ERP."""
        try:
            from ledgerhand.browser.skills import read_po
            return read_po(self.driver, self.config, po_no)
        except Exception:
            pass
        return None

    def _list_existing_bills(self, vendor_id: str) -> list[dict]:
        """Read all existing bills for this vendor from ERP."""
        try:
            from ledgerhand.browser.skills import list_vendor_bills
            return list_vendor_bills(self.driver, self.config, vendor_id)
        except Exception:
            return []

    # ─────────────────────────────────────────────────────────────────────
    # Payment helpers
    # ─────────────────────────────────────────────────────────────────────

    def _execute_payment(
        self,
        bk: str,
        parsed,
        spec: GoalSpec,
        task_id: str,
        approved: bool,
    ) -> None:
        """Execute a payment mutation for an already-entered bill."""
        from ledgerhand.parse import next_business_day
        from datetime import date
        pay_date = str(next_business_day(date.today()))

        pay_bk = business_key("pay", parsed.vendor_id or "", normalize_invoice_no(parsed.invoice_no_raw))
        pay_params = {
            "bill_id": bk,
            "amount_paise": parsed.total_paise,
            "pay_date": pay_date,
            "vendor_account_id": "",  # filled by prepare_payment from master
            "company_account_id": "",
            "marker": "",
        }
        try:
            execute_mutation(
                "schedule_payment", pay_bk, pay_params,
                self.driver, self.ledger, spec, self.config,
                self.run_id, task_id,
            )
        except NeedsHuman as e:
            self.ledger.log_event(self.run_id, "payment_needs_human", task_id, {"reason": str(e)})

    def _request_approval(self, bk: str, parsed, task_id: str, now: str) -> dict:
        """Write approval row and return payload dict."""
        from ledgerhand.parse import next_business_day
        from datetime import date, timedelta
        pay_date = str(next_business_day(date.today()))
        pay_bk = business_key("pay", parsed.vendor_id or "", normalize_invoice_no(parsed.invoice_no_raw))

        payload = {
            "kind": "schedule_payment",
            "bill_business_key": bk,
            "amount_paise": parsed.total_paise,
            "pay_date": pay_date,
            "vendor_account_id": "",
            "company_account_id": "",
            "operation_key": operation_key(
                "schedule_payment", pay_bk,
                hash_params({"bill_id": bk, "pay_date": pay_date}),
            ),
        }
        a_hash = action_hash(payload)
        expires_at = (
            datetime.datetime.now(datetime.timezone.utc) +
            datetime.timedelta(hours=self.config.approval_ttl_hours)
        ).isoformat()
        op_key = payload["operation_key"]

        self.ledger.conn.execute(
            "INSERT INTO approvals "
            "(run_id, task_id, operation_key, action_hash, payload_json, "
            " status, requested_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            (self.run_id, task_id, op_key, a_hash,
             canonical_json(payload), now, expires_at),
        )
        self.ledger.log_event(self.run_id, "approval_requested", task_id, {
            "amount_paise": parsed.total_paise,
            "action_hash": a_hash,
        })
        return payload

    # ─────────────────────────────────────────────────────────────────────
    # Approval wait loop
    # ─────────────────────────────────────────────────────────────────────

    def _wait_for_approvals(self, pending_tasks: list, spec: GoalSpec) -> None:
        """Poll for approve/reject commands; execute approved payments.

        Never marks 'executed' without actually executing the payment mutation.
        """
        wait_s = getattr(self.config, "wait_for_approvals_s", 120)
        start_t = time.time()

        while time.time() - start_t < wait_s:
            cmd = self._check_commands()
            if cmd == "stop":
                return

            # Check for newly approved approvals
            cur = self.ledger.conn.execute(
                "SELECT id, task_id, operation_key, action_hash, payload_json, expires_at "
                "FROM approvals WHERE run_id=? AND status='approved'",
                (self.run_id,),
            )
            for row in cur.fetchall():
                approval_id, task_id, op_key, a_hash, payload_json, expires_at = row

                # Re-validate before executing
                now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
                if expires_at and now_utc > expires_at:
                    self.ledger.conn.execute(
                        "UPDATE approvals SET status='expired' WHERE id=?", (approval_id,)
                    )
                    self.ledger.log_event(self.run_id, "approval_expired", task_id, {})
                    continue

                payload = json.loads(payload_json) if payload_json else {}
                computed_hash = hash_params(payload)
                if computed_hash != a_hash:
                    self.ledger.conn.execute(
                        "UPDATE approvals SET status='invalid_hash' WHERE id=?", (approval_id,)
                    )
                    self.ledger.log_event(self.run_id, "approval_hash_invalid", task_id, {})
                    continue

                # Find matching pending task
                task_info = next((t for t in pending_tasks if t["task_id"] == task_id), None)
                if task_info is None:
                    continue

                parsed = task_info["parsed"]
                bk = task_info["business_key"]
                self._execute_payment(bk, parsed, spec, task_id, approved=True)

                # Mark executed ONLY after mutation succeeds
                self.ledger.conn.execute(
                    "UPDATE approvals SET status='executed' WHERE id=?", (approval_id,)
                )
                self.ledger.log_event(self.run_id, "approval_executed", task_id, {
                    "amount_paise": payload.get("amount_paise"),
                })

            time.sleep(1)

    # ─────────────────────────────────────────────────────────────────────
    # Control-DB polling
    # ─────────────────────────────────────────────────────────────────────

    def _check_commands(self) -> str:
        """Poll control.db at safe points. Returns command kind or '' if none."""
        if not self.control_conn:
            return ""

        try:
            cur = self.control_conn.execute(
                "SELECT id, kind, payload_json FROM commands "
                "WHERE run_id=? AND id>? ORDER BY id ASC",
                (self.run_id, self.last_command_id),
            )
        except Exception:
            return ""

        for row in cur.fetchall():
            self.last_command_id = row[0]
            kind = row[1]
            payload = json.loads(row[2]) if row[2] else {}

            self.ledger.log_event(self.run_id, f"cmd_{kind}", "", payload)

            if kind in ("pause", "resume", "stop"):
                return kind
            elif kind == "approve":
                approval_id = payload.get("approval_id")
                now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                # Validate before accepting
                cur2 = self.ledger.conn.execute(
                    "SELECT operation_key, action_hash, expires_at FROM approvals "
                    "WHERE id=? AND status='pending'",
                    (approval_id,),
                )
                row2 = cur2.fetchone()
                if row2:
                    op_key, a_hash, expires_at = row2
                    if expires_at and now > expires_at:
                        self.ledger.conn.execute(
                            "UPDATE approvals SET status='expired' WHERE id=?", (approval_id,)
                        )
                    else:
                        self.ledger.conn.execute(
                            "UPDATE approvals SET status='approved', decided_at=? WHERE id=?",
                            (now, approval_id),
                        )
            elif kind == "reject":
                approval_id = payload.get("approval_id")
                now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                self.ledger.conn.execute(
                    "UPDATE approvals SET status='rejected', decided_at=? WHERE id=?",
                    (now, approval_id),
                )

        return ""

    def _finalize_run(self, status: str, verdict: str) -> None:
        """Persist final run status and verdict to ledger."""
        ended_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.ledger.conn.execute(
            "UPDATE runs SET status=?, verdict=?, ended_at=? WHERE id=?",
            (status, verdict, ended_at, self.run_id),
        )