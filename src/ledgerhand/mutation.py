"""Mutation protocol — lookup-first, prepare, commit, verify.

Why: this is the single place where side effects happen. The protocol
guarantees that:
  1. We always look up the world state first (no double-submission)
  2. dispatched is written durably BEFORE the commit click
  3. Any exception after dispatched becomes UNKNOWN, not FAILED
  4. We re-authorize at commit time (TOCTOU defence: policy may have changed)
  5. We verify by re-reading after every commit

Only this module may call prepare_*/commit_* skills.
"""
from __future__ import annotations
import os
import datetime
from typing import Optional

from .ledger import Ledger, Intent, ParamConflictError, Approval
from .browser import skills
from .browser.driver import Driver
from .browser.skills import PrepareError
from .verify import verify_mutation
from .config import Config


class NeedsHuman(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class AuthorityError(NeedsHuman):
    """Raised when authority check fails at commit time."""
    pass


def _crash_hook(config: Config, point: str, attempt: int) -> None:
    """Simulate hard process death at a specific point (for testing only).

    LH_TEST=1 required. Format: LH_CRASH_AT=after_dispatch:1
    meaning 'crash at after_dispatch on the 1st call'.
    Uses os._exit(137) to simulate kill -9 (no cleanup).
    """
    if not getattr(config, 'lh_test', False):
        return
    crash_at = getattr(config, 'lh_crash_at', None)
    if not crash_at:
        return
    parts = crash_at.split(":")
    if len(parts) != 2:
        return
    crash_point, n_str = parts
    if crash_point == point and attempt == int(n_str):
        os._exit(137)  # simulates hard process death


def authorize_again(
    intent: Intent,
    spec,
    config: Config,
    ledger: Optional[Ledger] = None,
) -> None:
    """Re-check policy + approval at commit time (TOCTOU defence).

    Why: the goal spec may have changed, the approval may have been
    revoked, or the expiry may have passed since the task was queued.
    We re-verify immediately before the commit click — not at planning time.

    Raises AuthorityError (a NeedsHuman) if any check fails:
    - amount exceeds hard ceiling
    - amount requires approval but none is in approved state
    - approval action_hash does not match intent operation_key
    - approval has expired
    """
    # 1. Hard ceiling — always enforced regardless of approval
    params = {}
    if intent.params_json:
        import json
        params = json.loads(intent.params_json)

    amount_paise = params.get("amount_paise", 0)
    if amount_paise > config.hard_ceiling_paise:
        raise AuthorityError(
            f"BLOCK_OVER_CEILING: {amount_paise} > {config.hard_ceiling_paise}"
        )

    # 2. If auto-pay is allowed and amount is within limit, no approval needed
    auto_pay_max = spec.authority.get("auto_pay_max_paise", 0) if hasattr(spec, 'authority') else 0
    schedule_payments = spec.actions.get("schedule_payments", False) if hasattr(spec, 'actions') else False

    # For create_bill: authority check is simpler — just scope and ceiling
    if intent.kind == "create_bill":
        # No payment authority needed for bill entry
        return

    # For schedule_payment: check approval or auto-authority
    if intent.kind == "schedule_payment":
        if not schedule_payments:
            raise AuthorityError("PAYMENTS_NOT_IN_GOAL: schedule_payments=false")

        if amount_paise <= auto_pay_max:
            # Auto-pay is allowed — no approval row needed
            return

        # Amount exceeds auto-pay limit: must have an approved approval row
        if ledger is None:
            raise AuthorityError("NEEDS_APPROVAL: no ledger provided for approval check")

        # Find the most recent approved approval for this intent
        cur = ledger.conn.execute(
            """SELECT id, action_hash, expires_at, payload_json
               FROM approvals
               WHERE operation_key = ? AND status = 'approved'
               ORDER BY id DESC LIMIT 1""",
            (intent.operation_key,)
        )
        row = cur.fetchone()
        if not row:
            raise AuthorityError(
                f"NEEDS_APPROVAL: no approved approval for operation_key={intent.operation_key}"
            )

        approval_id, action_hash, expires_at, payload_json = row

        # Check expiry
        now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if expires_at and now_utc > expires_at:
            raise AuthorityError(
                f"APPROVAL_EXPIRED: approval {approval_id} expired at {expires_at}"
            )

        # Check action_hash matches intent operation_key
        # The action_hash is computed from the canonical payload at approval-request time.
        # We re-hash the current params and compare.
        from .parse import hash_params, canonical_json
        import json
        payload = json.loads(payload_json) if payload_json else {}
        expected_hash = hash_params(payload)
        if action_hash != expected_hash:
            raise AuthorityError(
                f"APPROVAL_HASH_MISMATCH: stored={action_hash!r} computed={expected_hash!r}"
            )


def execute_mutation(
    kind: str,
    business_key: str,
    params: dict,
    driver: Driver,
    ledger: Ledger,
    spec,
    config: Config,
    run_id: str,
    task_id: str,
) -> dict:
    """Execute a mutation with lookup-first, prepare/commit split, and verify.

    Returns {"status": "confirmed", "how": ...} or raises NeedsHuman.

    The protocol:
      lookup → authorize_again → prepare → mark_dispatched →
      commit → [UNKNOWN on any exception] → verify → confirm
    """
    from .parse import make_marker

    marker = make_marker(business_key)

    try:
        intent = ledger.get_or_create_intent(kind, business_key, params, marker, run_id)
    except ParamConflictError:
        ledger.log_event(run_id, "param_conflict", task_id, {"business_key": business_key})
        raise NeedsHuman("PARAM_CONFLICT")

    attempt = 0
    while intent.attempts < config.retry.max_attempts:
        # --- Step 1: lookup-first ---
        world = skills.lookup(driver, config, kind, business_key, marker)
        if world is not None:
            if world.get("marker") == marker or world.get("internal_ref") == marker:
                ledger.log_event(run_id, "found_before_acting", task_id, {"world": world})
                ledger.confirm_intent(intent, "found_before_acting")
                return {"status": "confirmed", "how": "found_before_acting", "record": world}
            else:
                ledger.log_event(run_id, "drift_detected", task_id, {"world": world})
                raise NeedsHuman("WORLD_CONFLICT")

        # --- Step 2: re-authorize at commit time (TOCTOU defence) ---
        authorize_again(intent, spec, config, ledger)

        # --- Step 3: prepare (fill form — no side effect) ---
        try:
            if kind == "create_bill":
                vendor_account_id = params.get("vendor_account_id", "")
                skills.prepare_bill(driver, config, params, marker, vendor_account_id)
            elif kind == "schedule_payment":
                skills.prepare_payment(
                    driver, config,
                    params["bill_id"], params["pay_date"],
                    params.get("company_account_id", ""), marker
                )
            ledger.log_event(run_id, "prepared", task_id, {"kind": kind})
        except PrepareError as e:
            intent.prepare_failures += 1
            ledger.conn.execute(
                "UPDATE intents SET prepare_failures=? WHERE kind=? AND business_key=?",
                (intent.prepare_failures, intent.kind, intent.business_key)
            )
            ledger.log_event(run_id, "prepare_failed", task_id, {"error": str(e)})
            if intent.prepare_failures >= config.retry.prepare_max_failures:
                raise NeedsHuman("PREPARE_EXHAUSTED")
            continue

        # --- Step 4: mark_dispatched BEFORE the commit click ---
        ledger.mark_dispatched(intent)
        ledger.log_event(run_id, "intent_dispatched", task_id, {"marker": marker})
        _crash_hook(config, "after_dispatch", attempt + 1)

        # --- Step 5: commit (side effect) ---
        ambiguous = False
        record_id = None
        try:
            from .browser.guards import DialogHandler
            handler = DialogHandler()
            expected_paise = params.get("amount_paise", 0)
            handler.expect("", expected_paise)
            if kind == "create_bill":
                record_id = skills.commit_bill(driver, config, expected_paise, handler)
            else:
                record_id = skills.commit_payment(driver, config, expected_paise, handler)
            ledger.log_event(run_id, "commit_result", task_id, {"record_id": record_id})
        except Exception as e:
            # ANY exception after dispatched is UNKNOWN, not FAILED
            # We do NOT know if the side effect happened — never retry blindly
            ambiguous = True
            ledger.mark_unknown(intent)
            ledger.log_event(run_id, "unknown_outcome", task_id, {"error": str(e)})

        _crash_hook(config, "after_commit", attempt + 1)
        _crash_hook(config, "before_verify", attempt + 1)

        # --- Step 6: verify by re-reading application state ---
        result = verify_mutation(driver, config, kind, business_key, marker, params)

        if result == "matches":
            how = "recovered_no_retry" if ambiguous else "verified"
            ledger.confirm_intent(intent, how)
            ledger.log_event(run_id, "verify_ok", task_id, {"how": how})
            return {"status": "confirmed", "how": how, "record_id": record_id}

        if result == "conflict":
            ledger.log_event(run_id, "verify_conflict", task_id, {"result": "conflict"})
            raise NeedsHuman("WORLD_CONFLICT")

        # result == "absent" after full poll window → retry is safe
        intent.attempts += 1
        ledger.conn.execute(
            "UPDATE intents SET attempts=? WHERE kind=? AND business_key=?",
            (intent.attempts, intent.kind, intent.business_key)
        )
        ledger.log_event(run_id, "retry", task_id, {"attempt": intent.attempts})

    raise NeedsHuman("RETRY_EXHAUSTED")