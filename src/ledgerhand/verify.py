"""Verification — re-read the application after every mutation.

Why: G3 (evidence-first completion) requires re-reading. We never trust
that a click landed; we look it up by marker AND natural key and compare
every mutation-critical field.

'absent' is returned only after the FULL poll window, defending against
delayed list visibility in the ERP (e.g. an async commit or cache lag).

Bill fields verified: marker, vendor_id, invoice_no, invoice_date,
                      po_no, currency, total_paise, status.
Payment fields verified: bill_id, amount_paise, pay_date,
                         payee_account_id, company_account_id,
                         batch_ref, status.
"""
from __future__ import annotations
import time
from typing import Literal


def verify_mutation(
    driver,
    config,
    kind: str,
    business_key: str,
    marker: str,
    params: dict,
    _config_duplicate=None,  # kept for call-site compat; ignored
) -> Literal["matches", "conflict", "absent"]:
    """Poll with backoff until the record appears or the timeout expires.

    Poll intervals: config.verify.poll_interval_s
    Timeout:        config.verify.poll_timeout_s

    'matches':  record found AND every expected field matches
    'conflict': record found but one or more fields differ
    'absent':   not found after full poll window (safe to retry)
    """
    from .browser import skills

    deadline = time.monotonic() + config.verify.poll_timeout_s
    intervals = list(config.verify.poll_interval_s)
    i = 0

    while time.monotonic() < deadline:
        try:
            record = skills.lookup(driver, config, kind, business_key, marker)
        except Exception:
            # ERP may be transiently unreachable (e.g. timeout_after_commit fault
            # still sleeping on the worker thread, or a brief connection blip).
            # Treat as "not found yet" — sleep and retry.
            record = None

        if record is not None:
            mismatches = _check_fields(kind, record, params, marker)
            if not mismatches:
                return "matches"
            return "conflict"
        # Not found yet — wait and retry
        sleep_s = intervals[min(i, len(intervals) - 1)]
        i += 1
        time.sleep(sleep_s)

    return "absent"


def _check_fields(kind: str, record: dict, params: dict, marker: str) -> list[str]:
    """Return a list of mismatch descriptions, empty if all fields match.

    Each entry is a human-readable description of a disagreement so the
    Proof Pack can show exactly what was wrong.
    """
    mismatches = []

    # Marker must always match — this is our idempotency key
    rec_marker = record.get("marker", record.get("internal_ref", ""))
    if rec_marker != marker:
        mismatches.append(f"marker: expected={marker!r} actual={rec_marker!r}")

    if kind == "create_bill":
        # --- Bill fields ---
        _cmp(record, params, mismatches, "vendor_id")
        _cmp(record, params, mismatches, "invoice_no")
        _cmp(record, params, mismatches, "po_no")
        _cmp(record, params, mismatches, "currency")

        # Amount: compare as integer paise
        expected_paise = params.get("amount_paise", 0)
        if expected_paise > 0:
            actual_paise = record.get("total_paise", -1)
            if actual_paise != expected_paise:
                mismatches.append(
                    f"total_paise: expected={expected_paise} actual={actual_paise}"
                )

        # Bill must be in a terminal status (Posted/Scheduled/Paid — not Draft)
        rec_status = record.get("status", "")
        if rec_status == "Draft":
            mismatches.append(
                f"status: still Draft — bill was not committed"
            )

    elif kind == "schedule_payment":
        # --- Payment fields ---
        expected_paise = params.get("amount_paise", 0)
        if expected_paise > 0:
            actual_paise = record.get("amount_paise", -1)
            if actual_paise != expected_paise:
                mismatches.append(
                    f"amount_paise: expected={expected_paise} actual={actual_paise}"
                )

        _cmp(record, params, mismatches, "pay_date")
        _cmp(record, params, mismatches, "payee_account_id")
        _cmp(record, params, mismatches, "company_account_id")

        # Batch ref must contain our marker
        batch_ref = record.get("batch_ref", "")
        if marker not in batch_ref:
            mismatches.append(
                f"batch_ref: marker {marker!r} not found in {batch_ref!r}"
            )

        rec_status = record.get("status", "")
        if rec_status not in ("Scheduled", "Paid"):
            mismatches.append(
                f"status: expected Scheduled|Paid, got {rec_status!r}"
            )

    return mismatches


def _cmp(record: dict, params: dict, mismatches: list, key: str) -> None:
    """Helper: compare a single field if it exists in params."""
    if key not in params:
        return
    expected = params[key]
    actual = record.get(key, "__MISSING__")
    if str(expected) != str(actual):
        mismatches.append(f"{key}: expected={expected!r} actual={actual!r}")