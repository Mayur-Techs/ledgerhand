"""Deterministic invoice checks — every check returns CheckResult with evidence.

Why: the model proposes; code disposes. All business-rule evaluation happens
here in pure Python, independently of the LLM. Each check returns structured
evidence so policy.py and the Proof Pack can show exactly why a decision was made.
"""
from __future__ import annotations
import re
from typing import Optional
from datetime import date

from .models import ParsedInvoice, VendorRecord, PORecord, CheckResult
from .parse import edit_distance, normalize_invoice_no


def check_math(invoice: ParsedInvoice, tolerance_paise: int = 100) -> CheckResult:
    """Verify subtotal + tax == total within rounding_tolerance_paise."""
    expected = invoice.subtotal_paise + invoice.tax_paise
    diff = abs(expected - invoice.total_paise)
    if diff == 0:
        return CheckResult(
            check="math", result="pass",
            expected=str(expected), actual=str(invoice.total_paise),
            evidence="Exact match",
        )
    if diff <= tolerance_paise:
        return CheckResult(
            check="math", result="pass",
            expected=str(expected), actual=str(invoice.total_paise),
            allowed=str(tolerance_paise), evidence=f"Within rounding tolerance ({diff} paise)",
        )
    return CheckResult(
        check="math", result="fail",
        expected=str(expected), actual=str(invoice.total_paise),
        allowed=str(tolerance_paise),
        evidence=f"Mismatch: off by {diff} paise",
    )


def check_vendor(
    invoice: ParsedInvoice,
    vendor: Optional[VendorRecord],
) -> CheckResult:
    """GSTIN exact match → vendor_id. Missing/unmatched → HOLD_VENDOR_UNKNOWN."""
    if not vendor:
        return CheckResult(
            check="vendor", result="fail",
            actual=invoice.vendor_gstin_text if hasattr(invoice, "vendor_gstin_text") else invoice.vendor_gstin,
            evidence="HOLD_VENDOR_UNKNOWN: no vendor with this GSTIN in master",
        )
    if invoice.vendor_gstin != vendor.gstin:
        return CheckResult(
            check="vendor", result="fail",
            expected=vendor.gstin, actual=invoice.vendor_gstin,
            evidence="HOLD_VENDOR_UNKNOWN: GSTIN mismatch",
        )
    return CheckResult(
        check="vendor", result="pass",
        expected=vendor.gstin, actual=invoice.vendor_gstin,
        evidence=f"Exact GSTIN match → vendor_id={vendor.id}",
    )


def check_po_vendor(po: Optional[PORecord], vendor_id: str) -> CheckResult:
    """PO must belong to the resolved vendor."""
    if not po:
        return CheckResult(check="po_vendor", result="pass", evidence="No PO provided")
    if po.vendor_id != vendor_id:
        return CheckResult(
            check="po_vendor", result="fail",
            expected=vendor_id, actual=po.vendor_id,
            evidence="HOLD_PO_VENDOR_MISMATCH: PO belongs to a different vendor",
        )
    return CheckResult(check="po_vendor", result="pass", evidence="PO vendor matches invoice vendor")


def check_po_exists(po: Optional[PORecord], require_po: bool) -> CheckResult:
    """PO must exist and be Open when required."""
    if require_po and not po:
        return CheckResult(
            check="po_exists", result="fail",
            evidence="HOLD_NO_PO: PO required but not found",
        )
    if po and po.status == "Closed":
        return CheckResult(
            check="po_exists", result="fail",
            expected="Open", actual="Closed",
            evidence="HOLD_NO_PO: PO is Closed",
        )
    return CheckResult(check="po_exists", result="pass", evidence="PO exists and Open (or not required)")


def check_price(invoice_line_rate: int, po_line_rate: int, tolerance_pct: float) -> CheckResult:
    """Unit price must not exceed PO rate by more than tolerance_pct (over-billing risk)."""
    if po_line_rate <= 0:
        return CheckResult(check="price", result="fail", evidence="PO rate is zero or negative")
    over_pct = (invoice_line_rate - po_line_rate) / po_line_rate * 100
    if over_pct <= tolerance_pct:
        return CheckResult(
            check="price", result="pass",
            expected=str(po_line_rate), actual=str(invoice_line_rate),
            allowed=f"{tolerance_pct}%",
            evidence=f"Over by {over_pct:.1f}% — within tolerance",
        )
    return CheckResult(
        check="price", result="fail",
        expected=str(po_line_rate), actual=str(invoice_line_rate),
        allowed=f"{tolerance_pct}%",
        evidence=f"HOLD_PO_MISMATCH: over PO rate by {over_pct:.1f}%",
    )


def check_qty(billed_qty: int, received_qty: int) -> CheckResult:
    """Billed qty must not exceed received qty (3-way match)."""
    if billed_qty <= received_qty:
        return CheckResult(
            check="qty", result="pass",
            expected=str(received_qty), actual=str(billed_qty),
            evidence="Billed qty ≤ received qty",
        )
    return CheckResult(
        check="qty", result="fail",
        expected=str(received_qty), actual=str(billed_qty),
        evidence=f"HOLD_PO_MISMATCH: billed {billed_qty}, only {received_qty} received",
    )


def check_bank(invoice_claimed_account: str, vendor_master_account: str) -> CheckResult:
    """Invoice bank claim vs vendor master.
    Missing claim → pass (use master, noted). Mismatch → HOLD_BANK_CHANGED.
    """
    if not invoice_claimed_account.strip():
        return CheckResult(
            check="bank", result="pass",
            expected=vendor_master_account, actual="(none claimed)",
            evidence="No bank claim on invoice — will use vendor master",
        )
    # Compare only digits (last 4 are typically shown)
    def digits(s: str) -> str:
        return re.sub(r"\D", "", s)

    c = digits(invoice_claimed_account)
    m = digits(vendor_master_account)
    # Match if one is a suffix of the other (masked account comparison)
    if c and m and (c == m or m.endswith(c) or c.endswith(m)):
        return CheckResult(
            check="bank", result="pass",
            expected=vendor_master_account, actual=invoice_claimed_account,
            evidence="Claimed account matches vendor master",
        )
    return CheckResult(
        check="bank", result="fail",
        expected=vendor_master_account, actual=invoice_claimed_account,
        evidence="HOLD_BANK_CHANGED: claimed account differs from vendor master",
    )


def check_duplicate(
    bk: str,
    marker: str,
    current_total_paise: int,
    existing_bills: list[dict],
    near_max_edit_distance: int = 2,
) -> CheckResult:
    """Four outcomes: OURS | EXACT | CONFLICT | NEAR | NONE.

    existing_bills: list of dicts with keys:
      marker, business_key, total_paise, vendor_id, invoice_no_normalized

    OURS:     ERP bill with our marker AND fields match → idempotent re-run
    EXACT:    same normalized business_key, same amount, not ours
    CONFLICT: same business_key, different amount
    NEAR:     same vendor, same amount, invoice_no within edit distance ≤ max
    NONE:     no match
    """
    # Parse vendor_id and invoice_no from the business key "bill:vendor_id|inv_no"
    try:
        _, rest = bk.split(":", 1)
        vendor_id, inv_no = rest.split("|", 1)
    except ValueError:
        vendor_id, inv_no = "", ""

    for bill in existing_bills:
        # OURS: our marker present and fields match
        if bill.get("marker") == marker:
            if bill.get("total_paise") == current_total_paise:
                return CheckResult(
                    check="duplicate", result="warn", evidence="OURS",
                    expected=str(current_total_paise), actual=str(bill.get("total_paise")),
                )
            # Marker matches but amount differs — something is wrong
            return CheckResult(
                check="duplicate", result="fail", evidence="CONFLICT",
                expected=str(current_total_paise), actual=str(bill.get("total_paise")),
            )

        # EXACT / CONFLICT: same business_key
        if bill.get("business_key") == bk:
            if bill.get("total_paise") == current_total_paise:
                return CheckResult(
                    check="duplicate", result="fail", evidence="EXACT",
                    expected=str(current_total_paise), actual=str(bill.get("total_paise")),
                )
            return CheckResult(
                check="duplicate", result="fail", evidence="CONFLICT",
                expected=str(current_total_paise), actual=str(bill.get("total_paise")),
            )

    # NEAR: same vendor + amount, invoice_no within edit distance
    for bill in existing_bills:
        if (
            bill.get("vendor_id") == vendor_id
            and bill.get("total_paise") == current_total_paise
        ):
            dist = edit_distance(inv_no, bill.get("invoice_no_normalized", ""))
            if 0 < dist <= near_max_edit_distance:
                return CheckResult(
                    check="duplicate", result="fail", evidence="NEAR",
                    expected=inv_no, actual=bill.get("invoice_no_normalized", ""),
                    allowed=str(near_max_edit_distance),
                )

    return CheckResult(check="duplicate", result="pass", evidence="NONE")


def check_injection(
    text: str,
    high_patterns: list[str],
    medium_patterns: list[str],
) -> CheckResult:
    """Injection signal detection — a heuristic, NOT a security boundary.

    High severity → QUARANTINE_INJECTION (returned as fail with evidence QUARANTINE).
    Medium severity → note only (warn).
    Text is labelled untrusted data; patterns are regex strings from policy.yaml.
    """
    for pat in high_patterns:
        if re.search(pat, text, re.IGNORECASE | re.MULTILINE):
            return CheckResult(
                check="injection", result="fail", evidence="QUARANTINE",
                actual=f"Matched high-severity pattern: {pat}",
            )
    for pat in medium_patterns:
        if re.search(pat, text, re.IGNORECASE | re.MULTILINE):
            return CheckResult(
                check="injection", result="warn", evidence="medium",
                actual=f"Matched medium-severity pattern: {pat}",
            )
    return CheckResult(check="injection", result="pass", evidence="Clean")


def check_currency(currency: str, supported: list[str] | None = None) -> CheckResult:
    """Only INR is supported in Tier 1."""
    supported = supported or ["INR"]
    if currency in supported:
        return CheckResult(check="currency", result="pass", evidence="Supported currency")
    return CheckResult(
        check="currency", result="fail",
        expected=str(supported), actual=currency,
        evidence="HOLD_UNSUPPORTED_FOREIGN_CURRENCY",
    )


def check_date(d: Optional[date]) -> CheckResult:
    """Date must be valid and parseable."""
    if d is not None:
        return CheckResult(check="date", result="pass", evidence=f"Valid date: {d}")
    return CheckResult(check="date", result="fail", evidence="Invalid or missing date")


def check_zero_total(total_paise: int) -> CheckResult:
    """Total must be positive."""
    if total_paise > 0:
        return CheckResult(check="zero_total", result="pass", evidence=f"Total: {total_paise} paise")
    return CheckResult(
        check="zero_total", result="fail",
        actual=str(total_paise), evidence="HOLD_MATH_ERROR: zero or negative total",
    )


def run_all_checks(
    invoice: ParsedInvoice,
    vendor: Optional[VendorRecord],
    po: Optional[PORecord],
    existing_bills: list[dict],
    config,
    invoice_raw_text: str = "",
) -> list[CheckResult]:
    """Run all checks and return results. Each check has structured evidence."""
    results: list[CheckResult] = []

    # Math and total sanity
    results.append(check_zero_total(invoice.total_paise))
    results.append(check_math(invoice, config.rounding_tolerance_paise))

    # Currency
    results.append(check_currency(invoice.currency, ["INR"]))

    # Vendor resolution
    results.append(check_vendor(invoice, vendor))

    # PO checks (only if vendor resolved)
    if vendor:
        results.append(check_po_vendor(po, vendor.id))
        results.append(check_po_exists(po, config.require_po))
        # Price and qty per line (simplified: compare first line)
        if po and invoice.lines and po.lines:
            results.append(check_price(
                invoice.lines[0].rate_paise,
                po.lines[0].rate_paise,
                config.price_tolerance_pct,
            ))
            results.append(check_qty(invoice.lines[0].qty, po.lines[0].received_qty))
        # Bank account
        master_account = ""
        if vendor.accounts:
            default = vendor.default_account()
            master_account = default.account_number if default else ""
        results.append(check_bank(invoice.bank_account_claimed, master_account))
    else:
        # No vendor — still run bank check (will note missing claim or fail)
        results.append(check_po_vendor(None, ""))
        results.append(check_po_exists(None, config.require_po))
        results.append(check_bank(invoice.bank_account_claimed, ""))

    # Duplicate detection
    bk = f"bill:{invoice.vendor_id or ''}|{invoice.invoice_no}"
    marker = ""  # caller should pass via existing_bills context
    results.append(check_duplicate(
        bk, marker, invoice.total_paise, existing_bills,
        config.near_dup_max_edit_distance,
    ))

    # Injection signals
    text_to_scan = invoice_raw_text or invoice.invoice_no
    results.append(check_injection(
        text_to_scan,
        list(config.injection_high),
        list(config.injection_medium),
    ))

    return results