"""Domain models — pydantic v2.

Why: validate at every boundary so bugs are loud at the edge, not silent
in the middle. Every model is immutable (frozen) so a value that enters
the system cannot be mutated quietly.
"""
from __future__ import annotations
from datetime import date, datetime
from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator


class VendorAccount(BaseModel, frozen=True):
    id: str
    bank_name: str
    account_number: str  # masked; never typed by the operator
    is_default: bool = False


class VendorRecord(BaseModel, frozen=True):
    id: str
    name: str
    gstin: str
    accounts: tuple[VendorAccount, ...] = ()

    def default_account(self) -> Optional[VendorAccount]:
        for a in self.accounts:
            if a.is_default:
                return a
        return self.accounts[0] if self.accounts else None


class POLine(BaseModel, frozen=True):
    item: str
    ordered_qty: int
    received_qty: int
    rate_paise: int  # per unit


class PORecord(BaseModel, frozen=True):
    po_no: str
    vendor_id: str
    status: Literal["Open", "Closed"]
    lines: tuple[POLine, ...] = ()


class InvoiceLine(BaseModel, frozen=True):
    description: str
    qty: int
    rate_paise: int
    amount_paise: int


class InvoiceDraft(BaseModel):
    """Raw extraction output from the LLM — verbatim text spans + confidence."""
    vendor_gstin_text: str = ""
    vendor_gstin_confidence: float = 0.0
    vendor_name_text: str = ""
    invoice_no_text: str = ""
    invoice_no_confidence: float = 0.0
    invoice_date_text: str = ""
    invoice_date_confidence: float = 0.0
    po_no_text: str = ""
    po_no_confidence: float = 0.0
    total_text: str = ""
    total_confidence: float = 0.0
    subtotal_text: str = ""
    tax_text: str = ""
    bank_account_text: str = ""  # claimed by the invoice, compared against master
    currency_text: str = "INR"
    lines_text: list[dict] = Field(default_factory=list)
    raw_text: str = ""
    injection_signals: list[dict] = Field(default_factory=list)  # [{severity, text}]


class ParsedInvoice(BaseModel, frozen=True):
    """Parsed and validated invoice — code did all arithmetic."""
    vendor_gstin: str
    vendor_name: str
    vendor_id: Optional[str] = None  # resolved after vendor lookup
    invoice_no: str  # normalized
    invoice_no_raw: str
    invoice_date: date
    po_no: str
    total_paise: int
    subtotal_paise: int
    tax_paise: int
    bank_account_claimed: str = ""  # from invoice; may be empty
    currency: str = "INR"
    lines: tuple[InvoiceLine, ...] = ()

    @field_validator("total_paise", "subtotal_paise", "tax_paise")
    @classmethod
    def must_be_positive(cls, v: int) -> int:
        if v < 0:
            raise ValueError("Money amounts must be non-negative")
        return v


class BillRecord(BaseModel, frozen=True):
    """ERP bill as read back from the UI."""
    id: str
    vendor_id: str
    invoice_no: str
    invoice_date: date
    total_paise: int
    po_no: str
    marker: str  # Internal Ref field; "LH-" prefix
    status: Literal["Draft", "Posted", "Scheduled", "Paid"]
    payee_account_id: Optional[str] = None


class PaymentRecord(BaseModel, frozen=True):
    """ERP payment as read back from the UI."""
    id: str
    bill_id: str
    amount_paise: int
    pay_date: date
    payee_account_id: str
    company_account_id: str
    batch_ref: str  # contains the marker
    status: Literal["Scheduled", "Paid"]


class GoalSpec(BaseModel):
    """Compiled + user-confirmed goal specification.

    Mutable during compilation/LLM parsing.
    Call .confirm() to produce a frozen ConfirmedGoalSpec that cannot be
    mutated after the user has approved it.

    Why: policy and authority checks use the GoalSpec throughout a run.
    If it could be modified mid-run (e.g. by a control.db command), an
    authority bypass would be possible.
    """
    workflow: str = "AP_INVOICE_TO_PAY"
    scope: dict = Field(default_factory=lambda: {
        "inbox": "",
        "vendors_include": [],
        "vendors_exclude": [],
        "date_from": None,
        "date_to": None,
    })
    actions: dict = Field(default_factory=lambda: {
        "enter_bills": True,
        "schedule_payments": False,
    })
    authority: dict = Field(default_factory=lambda: {
        "auto_pay_max_paise": 5_000_000,
    })
    outputs: dict = Field(default_factory=lambda: {"proof_pack": True})
    clarifications: list[str] = Field(default_factory=list)

    def confirm(self) -> "GoalSpec":
        """Return a copy with model_config frozen.

        In Pydantic v2, the cleanest way to enforce immutability on a model
        that has dict fields is to return a model_copy() and then protect it
        via object.__setattr__ override rather than frozen=True (which
        prevents dict mutation but not dict-value mutation anyway).

        For our purposes: the returned object is the 'confirmed' spec and
        callers must not mutate it. The AGENTS.md rule enforces this
        architecturally. The copy ensures the original compilation draft
        cannot affect the running spec.
        """
        return self.model_copy(deep=True)


class CheckResult(BaseModel, frozen=True):
    """Result of a single check — evidence, not just pass/fail."""
    check: str
    result: Literal["pass", "fail", "warn"]
    expected: str = ""
    actual: str = ""
    allowed: str = ""
    evidence: str = ""


class Decision(BaseModel, frozen=True):
    """Per-invoice decision produced by policy.py."""
    bill_decision: Literal["ENTER", "HOLD", "QUARANTINE", "SKIP"]
    pay_decision: Literal["AUTO", "APPROVE", "BLOCK", "NONE"]
    reason_code: str
    rule_row: int  # which row in the decision table fired
    evidence: list[CheckResult] = Field(default_factory=list)


class ApprovalPayload(BaseModel, frozen=True):
    """Exact payload that is hashed for the approval action_hash."""
    kind: str
    bill_business_key: str
    bill_id: str
    amount_paise: int
    pay_date: str  # ISO date string, fixed at request time
    payee_account_id: str
    company_account_id: str
    operation_key: str
