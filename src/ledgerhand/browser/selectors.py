"""Locator ladder — per-element fallback selectors for v1 and v2 UI.

Why: the locator ladder defends against UI drift. We try the most
semantic selector first (role+name), then label, then text, then
restricted CSS. We NEVER use fragile CSS ids as the first choice.
"""
import re

class BLOCKEDUIAmbiguous(Exception):
    pass

BILL_SUBMIT_BUTTON = [
    ("role", {"role": "button", "name": re.compile(r"Submit Bill|Post Bill", re.I)}),
    ("text", "Submit Bill"),
    ("text", "Post Bill"),
]

INTERNAL_REF_FIELD = [
    ("label", "Internal Ref"),
    ("label", "Internal Reference"),
    ("placeholder", "Internal Ref"),
    ("css", "input[name='internal_ref']"),
    ("css", "input[name='internal_reference']"),
]

INVOICE_NO_FIELD = [
    ("label", "Invoice No"),
    ("label", "Invoice Number"),
    ("css", "input[name='invoice_no']"),
]

VENDOR_SELECT = [
    ("label", "Vendor"),
    ("css", "select[name='vendor_id']"),
]

PO_NO_FIELD = [
    ("label", "PO No"),
    ("label", "Purchase Order No"),
    ("css", "input[name='po_no']"),
]

INVOICE_DATE_FIELD = [
    ("label", "Invoice Date"),
    ("css", "input[name='invoice_date']"),
]

BANK_ACCOUNT_SELECT = [
    ("label", "Bank Account"),
    ("css", "select[name='bank_account_id']"),
]

PAYMENT_BILL_CHECKBOX = [
    ("role", {"role": "checkbox"}),  # first visible enabled checkbox on payment form
]

PAY_DATE_FIELD = [
    ("label", "Pay Date"),
    ("css", "input[name='pay_date']"),
]

BATCH_REF_FIELD = [
    ("label", "Batch Ref"),
    ("css", "input[name='batch_ref']"),
]

SCHEDULE_PAYMENT_BUTTON = [
    ("role", {"role": "button", "name": re.compile(r"Schedule Payment", re.I)}),
    ("text", "Schedule Payment"),
]

def resolve_locator(page, ladder: list, name: str):
    """Try each entry in the ladder. Return the first locator with exactly
    one visible, enabled match. If ambiguous or not found on a money-moving
    element, raise BLOCKED_UI_AMBIGUOUS.
    """
    for strategy, selector in ladder:
        if strategy == "role":
            loc = page.get_by_role(selector["role"], name=selector.get("name"))
        elif strategy == "text":
            loc = page.get_by_text(selector, exact=True)
        elif strategy == "label":
            loc = page.get_by_label(selector)
        elif strategy == "placeholder":
            loc = page.get_by_placeholder(selector)
        elif strategy == "css":
            loc = page.locator(selector)
        else:
            continue
            
        if loc.count() == 1 and loc.is_visible() and loc.is_enabled():
            return loc
            
    raise BLOCKEDUIAmbiguous(f"Could not resolve single locator for {name}")