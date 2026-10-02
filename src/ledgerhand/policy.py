"""Decision engine — first-match rule table.

Why: every decision is deterministic, auditable, and line-by-line explainable.
The rule that fired is stored so the Proof Pack can show exactly why. The model
never makes decisions; this function does.
"""
from __future__ import annotations
from typing import Optional

from .models import Decision, ParsedInvoice, GoalSpec, CheckResult
from .config import Config


# ── helpers ─────────────────────────────────────────────────────────────────

def _find(checks: list[CheckResult], check_name: str) -> Optional[CheckResult]:
    for c in checks:
        if c.check == check_name:
            return c
    return None


def _failed(checks: list[CheckResult], check_name: str) -> bool:
    c = _find(checks, check_name)
    return c is not None and c.result == "fail"


def _evidence_str(checks: list[CheckResult], *names: str) -> str:
    return "; ".join(
        f"{c.check}={c.evidence}" for c in checks if c.check in names
    )


# ── scope check ─────────────────────────────────────────────────────────────

def _in_scope(invoice: ParsedInvoice, spec: GoalSpec) -> bool:
    """Return True if this invoice is in scope for this goal."""
    inc = spec.scope.get("vendors_include", [])
    exc = spec.scope.get("vendors_exclude", [])
    if inc and invoice.vendor_id not in inc:
        return False
    if exc and invoice.vendor_id in exc:
        return False
    # date range: spec.scope["date_from"] / ["date_to"] as ISO strings or None
    df = spec.scope.get("date_from")
    dt = spec.scope.get("date_to")
    if df and invoice.invoice_date.isoformat() < df:
        return False
    if dt and invoice.invoice_date.isoformat() > dt:
        return False
    return True


# ── pay decision ─────────────────────────────────────────────────────────────

def _pay_decision(
    total_paise: int,
    spec: GoalSpec,
    config: Config,
) -> tuple[str, str]:
    """Return (pay_decision, reason_code) for the payment step."""
    if not spec.actions.get("schedule_payments", False):
        return "NONE", "OK_NO_PAYMENT_REQUESTED"
    auto_max = spec.authority.get("auto_pay_max_paise", 0)
    if total_paise <= auto_max:
        return "AUTO", "OK_AUTO_PAY"
    if total_paise <= config.hard_ceiling_paise:
        return "APPROVE", "NEEDS_APPROVAL_AMOUNT"
    return "BLOCK", "BLOCK_OVER_CEILING"


# ── main decision function ───────────────────────────────────────────────────

def decide(
    checks: list[CheckResult],
    invoice: ParsedInvoice,
    spec: GoalSpec,
    config: Config,
    existing_intent=None,
) -> Decision:
    """First-match decision table. Returns Decision with rule_row and evidence.

    Table (from PLAN §6.3):
      1  QUARANTINE: high injection, low confidence on critical field
      2  SKIP: outside goal scope
      3  OURS: our confirmed intent → re-run path, continue to pay step
      4  HOLD: unsupported type, vendor unknown, PO/vendor mismatch, math error, date ambiguous
      5  HOLD: EXACT duplicate
      6  HOLD: CONFLICT duplicate (same key, different amount)
      7  HOLD: NEAR duplicate
      8  HOLD: bank account changed (always_human)
      9  HOLD: no PO, price > tolerance, qty > received, PO closed
      10 ENTER: all checks pass — pay tier decided by authority
    """
    # ── Row 1: QUARANTINE ────────────────────────────────────────────────────
    inj = _find(checks, "injection")
    if inj and inj.result == "fail" and inj.evidence == "QUARANTINE":
        return Decision(
            bill_decision="QUARANTINE", pay_decision="NONE",
            reason_code="QUARANTINE_INJECTION", rule_row=1,
            evidence=checks,
        )
    # Low confidence on critical fields is also a quarantine-level hold
    # (treated as HOLD rather than QUARANTINE per the plan's row 1 wording)
    # "critical field low confidence" → HOLD_EXTRACTION_LOW_CONF
    # (confidence is checked upstream; we look for a dedicated check result)
    conf = _find(checks, "confidence")
    if conf and conf.result == "fail":
        return Decision(
            bill_decision="QUARANTINE", pay_decision="NONE",
            reason_code="HOLD_EXTRACTION_LOW_CONF", rule_row=1,
            evidence=checks,
        )

    # ── Row 2: SKIP — outside scope ──────────────────────────────────────────
    if not _in_scope(invoice, spec):
        return Decision(
            bill_decision="SKIP", pay_decision="NONE",
            reason_code="OUT_OF_SCOPE", rule_row=2,
            evidence=checks,
        )

    # ── Row 3: OURS — confirmed intent, idempotent re-run ───────────────────
    dup = _find(checks, "duplicate")
    if dup and dup.evidence == "OURS":
        if existing_intent and existing_intent.status == "confirmed":
            # Continue to pay step — bill is already confirmed
            pay, pay_code = _pay_decision(invoice.total_paise, spec, config)
            return Decision(
                bill_decision="SKIP", pay_decision=pay,
                reason_code="SKIPPED_ALREADY_DONE", rule_row=3,
                evidence=checks,
            )
        # Marker found but intent not confirmed — treat as re-run in progress
        return Decision(
            bill_decision="SKIP", pay_decision="NONE",
            reason_code="SKIPPED_ALREADY_DONE", rule_row=3,
            evidence=checks,
        )

    # ── Row 4: HOLD — unsupported types ─────────────────────────────────────
    curr = _find(checks, "currency")
    if curr and curr.result == "fail":
        return Decision(
            bill_decision="HOLD", pay_decision="NONE",
            reason_code="HOLD_UNSUPPORTED_FOREIGN_CURRENCY", rule_row=4,
            evidence=checks,
        )
    zero = _find(checks, "zero_total")
    if zero and zero.result == "fail":
        return Decision(
            bill_decision="HOLD", pay_decision="NONE",
            reason_code="HOLD_MATH_ERROR", rule_row=4,
            evidence=checks,
        )
    math_c = _find(checks, "math")
    if math_c and math_c.result == "fail":
        return Decision(
            bill_decision="HOLD", pay_decision="NONE",
            reason_code="HOLD_MATH_ERROR", rule_row=4,
            evidence=checks,
        )
    date_c = _find(checks, "date")
    if date_c and date_c.result == "fail":
        return Decision(
            bill_decision="HOLD", pay_decision="NONE",
            reason_code="HOLD_DATE_AMBIGUOUS", rule_row=4,
            evidence=checks,
        )
    vendor_c = _find(checks, "vendor")
    if vendor_c and vendor_c.result == "fail":
        return Decision(
            bill_decision="HOLD", pay_decision="NONE",
            reason_code="HOLD_VENDOR_UNKNOWN", rule_row=4,
            evidence=checks,
        )
    po_vendor_c = _find(checks, "po_vendor")
    if po_vendor_c and po_vendor_c.result == "fail":
        return Decision(
            bill_decision="HOLD", pay_decision="NONE",
            reason_code="HOLD_PO_VENDOR_MISMATCH", rule_row=4,
            evidence=checks,
        )

    # ── Row 5: HOLD — exact duplicate ────────────────────────────────────────
    if dup and dup.result == "fail" and dup.evidence == "EXACT":
        return Decision(
            bill_decision="HOLD", pay_decision="NONE",
            reason_code="HOLD_DUPLICATE_EXACT", rule_row=5,
            evidence=checks,
        )

    # ── Row 6: HOLD — conflict duplicate (same key, different amount) ────────
    if dup and dup.result == "fail" and dup.evidence == "CONFLICT":
        return Decision(
            bill_decision="HOLD", pay_decision="NONE",
            reason_code="HOLD_BUSINESS_KEY_CONFLICT", rule_row=6,
            evidence=checks,
        )

    # ── Row 7: HOLD — near duplicate ─────────────────────────────────────────
    if dup and dup.result == "fail" and dup.evidence == "NEAR":
        return Decision(
            bill_decision="HOLD", pay_decision="NONE",
            reason_code="HOLD_DUPLICATE_NEAR", rule_row=7,
            evidence=checks,
        )

    # ── Row 8: HOLD — bank account changed (always_human) ───────────────────
    bank_c = _find(checks, "bank")
    if bank_c and bank_c.result == "fail":
        return Decision(
            bill_decision="HOLD", pay_decision="NONE",
            reason_code="HOLD_BANK_CHANGED", rule_row=8,
            evidence=checks,
        )

    # ── Row 9: HOLD — PO / price / qty issues ────────────────────────────────
    po_c = _find(checks, "po_exists")
    if po_c and po_c.result == "fail":
        return Decision(
            bill_decision="HOLD", pay_decision="NONE",
            reason_code="HOLD_NO_PO", rule_row=9,
            evidence=checks,
        )
    price_c = _find(checks, "price")
    if price_c and price_c.result == "fail":
        return Decision(
            bill_decision="HOLD", pay_decision="NONE",
            reason_code="HOLD_PO_MISMATCH", rule_row=9,
            evidence=checks,
        )
    qty_c = _find(checks, "qty")
    if qty_c and qty_c.result == "fail":
        return Decision(
            bill_decision="HOLD", pay_decision="NONE",
            reason_code="HOLD_PO_MISMATCH", rule_row=9,
            evidence=checks,
        )

    # ── Row 10: ENTER — all checks pass ──────────────────────────────────────
    pay, pay_code = _pay_decision(invoice.total_paise, spec, config)
    return Decision(
        bill_decision="ENTER", pay_decision=pay,
        reason_code=pay_code, rule_row=10,
        evidence=checks,
    )