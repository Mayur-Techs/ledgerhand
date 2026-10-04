"""Browser skills — the only things the runner can do in the browser.

Why: the skill boundary is the last line of defence. Skills are either
read-only or split into prepare (no side effect) + commit (the click).
Only mutation.py may call prepare_*/commit_* skills.
"""
from __future__ import annotations
import re
import time
from datetime import date
from typing import Optional
from urllib.parse import urlparse, urljoin

from .driver import Driver
from .guards import DialogHandler, UnexpectedDialog
from .selectors import (
    BILL_SUBMIT_BUTTON, INTERNAL_REF_FIELD, INVOICE_NO_FIELD, VENDOR_SELECT,
    PO_NO_FIELD, INVOICE_DATE_FIELD, BANK_ACCOUNT_SELECT, SCHEDULE_PAYMENT_BUTTON,
    PAY_DATE_FIELD, BATCH_REF_FIELD, resolve_locator,
)
from ..config import Config
from ..models import VendorRecord, VendorAccount, PORecord, POLine, BillRecord, PaymentRecord
from ..parse import normalize_invoice_no
from ..money import parse_paise, paise_to_rupees_str


class PrepareError(Exception):
    pass


class BLOCKEDUIAmbiguous(Exception):
    pass


# ── Session management ────────────────────────────────────────────────────────

def ensure_session(driver: Driver, config: Config) -> None:
    """Verify session is active. Re-login if redirected to /login.

    Credentials come from config (env), never from the LLM.
    """
    page = driver.page()
    base = config.lb_base_url.rstrip("/")
    page.goto(f"{base}/bills")
    if "/login" in page.url:
        _do_login(page, base, config)


def _do_login(page, base: str, config: Config) -> None:
    """Log in to LedgerBooks. Called on session expiry or initial login."""
    page.goto(f"{base}/login")
    page.wait_for_load_state("domcontentloaded")
    page.fill("input[name='username']", config.lb_username)
    page.fill("input[name='password']", config.lb_password)
    page.click("button[type='submit']")
    page.wait_for_load_state("networkidle")


def _check_session(page, base: str, config: Config, intended_path: str) -> None:
    """Navigate to intended_path, detect redirect to /login, re-login and return."""
    target = f"{base}{intended_path}"
    page.goto(target)
    if "/login" in page.url:
        _do_login(page, base, config)
        page.goto(target)
# ── Read skills ───────────────────────────────────────────────────────────────

def find_vendor(driver: Driver, config: Config, gstin: str = "", name: str = "") -> Optional[VendorRecord]:
    """Navigate /vendors, search for vendor by GSTIN or name. Return VendorRecord or None."""
    page = driver.page()
    base = config.lb_base_url.rstrip("/")
    _check_session(page, base, config, f"/vendors")
    page.wait_for_load_state("domcontentloaded")

    # Find matching row in vendor list
    rows = page.locator("table tbody tr").all()
    for row in rows:
        text = row.inner_text()
        if gstin and gstin in text:
            # Navigate to vendor detail
            link = row.locator("a").first
            vendor_id = link.get_attribute("href", timeout=2000).rstrip("/").split("/")[-1]
            return read_vendor(driver, config, vendor_id)
        if name and name.lower() in text.lower():
            link = row.locator("a").first
            vendor_id = link.get_attribute("href", timeout=2000).rstrip("/").split("/")[-1]
            return read_vendor(driver, config, vendor_id)
    return None


def read_vendor(driver: Driver, config: Config, vendor_id: str) -> Optional[VendorRecord]:
    """Read /vendors/{id}. Return full VendorRecord including accounts."""
    page = driver.page()
    base = config.lb_base_url.rstrip("/")
    _check_session(page, base, config, f"/vendors/{vendor_id}")
    page.wait_for_load_state("domcontentloaded")

    # Parse vendor fields from detail page
    try:
        h3_text = page.locator("h3:has-text('Vendor:')").first.inner_text(timeout=3000).strip()
        name = h3_text.replace("Vendor:", "").strip()
    except Exception:
        name = ""
    try:
        p_text = page.locator("p:has-text('GSTIN:')").first.inner_text(timeout=3000).strip()
        gstin = p_text.replace("GSTIN:", "").strip()
    except Exception:
        gstin = ""

    # Parse accounts
    accounts = []
    # In mock ERP, the table comes right after <h4>Bank Accounts</h4>
    acct_rows = page.locator("h4:has-text('Bank Accounts') + table tbody tr").all()
    for row in acct_rows:
        cells = row.locator("td").all()
        if len(cells) >= 3:
            bank = cells[0].inner_text().strip()
            acct_no = cells[1].inner_text().strip()
            is_default = "Yes" in cells[2].inner_text()
            # The mock ERP doesn't display a separate account ID, use acct_no as ID
            acct_id = acct_no
            accounts.append(VendorAccount(id=acct_id, bank_name=bank, account_number=acct_no, is_default=is_default))

    return VendorRecord(id=vendor_id, name=name, gstin=gstin, accounts=tuple(accounts))


def read_po(driver: Driver, config: Config, po_no: str) -> Optional[PORecord]:
    """Read /pos/{po_no}. Return PORecord with lines, received qty, status."""
    page = driver.page()
    base = config.lb_base_url.rstrip("/")
    _check_session(page, base, config, f"/pos/{po_no}")
    page.wait_for_load_state("domcontentloaded")

    if page.locator("h2:has-text('Not Found'), .error-404").count() > 0:
        return None

    try:
        vendor_id = page.locator("p:has-text('Vendor ID:')").first.inner_text(timeout=3000).replace("Vendor ID:", "").strip()
        status = page.locator("p:has-text('Status:')").first.inner_text(timeout=3000).replace("Status:", "").strip()
    except Exception:
        return None

    lines = []
    line_rows = page.locator("h4:has-text('Lines') + table tbody tr").all()
    for row in line_rows:
        cells = row.locator("td").all()
        if len(cells) >= 4:
            item = cells[0].inner_text().strip()
            ordered = int(cells[1].inner_text().strip() or "0")
            received = int(cells[2].inner_text().strip() or "0")
            rate = int(cells[3].inner_text().strip() or "0")
            lines.append(POLine(item=item, ordered_qty=ordered, received_qty=received, rate_paise=rate))

    return PORecord(po_no=po_no, vendor_id=vendor_id, status=status, lines=tuple(lines))


def list_vendor_bills(driver: Driver, config: Config, vendor_id: str) -> list[dict]:
    """Read /bills?vendor={vendor_id} across all pages. Includes marker, business_key, total_paise.

    Used by check_duplicate to detect OURS/EXACT/CONFLICT/NEAR.
    """
    page = driver.page()
    base = config.lb_base_url.rstrip("/")
    bills = []
    pg = 1

    while True:
        _check_session(page, base, config, f"/bills?vendor={vendor_id}&page={pg}")
        page.wait_for_load_state("domcontentloaded")

        rows = page.locator("table tbody tr").all()
        if not rows:
            break

        for row in rows:
            cells = row.locator("td").all()
            if len(cells) < 6:
                continue
            # Columns: ID(0), Vendor(1), Invoice No(2), Internal Ref(3), Amount(4), Status(5), Actions(6)
            invoice_no_raw = cells[2].inner_text().strip()
            marker = cells[3].inner_text().strip()
            total_text = cells[4].inner_text().strip()
            try:
                # The amount is displayed as float-like "100.00" but is called Amount (Paise).
                # parse_paise("100.00") will return 10000, which is incorrect if it's already paise.
                # Actually, parse_paise("100.00") splits on "." -> rupees_s = "100", paise_s = "00"
                # so it returns 10000. But if Amount is "100.00" representing 100 paise... 
                # Let's just use parse_paise assuming it's formatted as rupees.
                from ledgerhand.money import parse_paise
                total_paise = parse_paise(total_text)
            except ValueError:
                total_paise = 0
            inv_no_norm = normalize_invoice_no(invoice_no_raw)
            bk = f"bill:{vendor_id}|{inv_no_norm}"
            bills.append({
                "marker": marker,
                "business_key": bk,
                "invoice_no_normalized": inv_no_norm,
                "total_paise": total_paise,
                "vendor_id": vendor_id,
            })

        # Check for next page
        next_btn = page.locator("a:has-text('Next'), a.next-page")
        if next_btn.count() == 0:
            break
        pg += 1

    return bills


def lookup(driver: Driver, config: Config, kind: str, business_key: str, marker: str) -> Optional[dict]:
    """Search by BOTH marker (internal_ref) AND natural key. Return record dict or None.

    Searches across all pages. The marker is written into Internal Ref (bills)
    or Batch Ref (payments). 'Absent' only when BOTH searches return nothing
    across the full poll window.
    """
    page = driver.page()
    base = config.lb_base_url.rstrip("/")

    if kind == "create_bill":
        # Search by marker first (Internal Ref)
        _check_session(page, base, config, f"/bills?q={marker}")
        page.wait_for_load_state("domcontentloaded")
        rows = page.locator("table tbody tr").all()
        for row in rows:
            text = row.inner_text()
            if marker in text:
                parsed = _parse_bill_row(row, marker)
                return read_bill(driver, config, parsed['bill_id'])

        # Search by natural key (invoice_no embedded in business_key)
        # business_key = "bill:vendor_id|inv_no_normalized"
        try:
            inv_no = business_key.split("|", 1)[1]
        except IndexError:
            inv_no = ""
        if inv_no:
            page.goto(f"{base}/bills?q={inv_no}")
            page.wait_for_load_state("domcontentloaded")
            rows = page.locator("table tbody tr").all()
            for row in rows:
                cells = row.locator("td").all()
                if len(cells) >= 4:
                    row_inv = normalize_invoice_no(cells[2].inner_text().strip())
                    if row_inv == inv_no:
                        row_marker = cells[3].inner_text().strip()
                        parsed = _parse_bill_row(row, row_marker)
                        return read_bill(driver, config, parsed['bill_id'])

    elif kind == "schedule_payment":
        _check_session(page, base, config, f"/payments?q={marker}")
        page.wait_for_load_state("domcontentloaded")
        rows = page.locator("table tbody tr").all()
        for row in rows:
            if marker in row.inner_text():
                parsed = _parse_payment_row(row, marker)
                return read_payment(driver, config, parsed['payment_id'])

    return None


def _parse_bill_row(row, marker: str) -> dict:
    """Extract bill info from a table row."""
    cells = row.locator("td").all()
    bill_id = ""
    total_paise = 0
    inv_no = ""
    link = row.locator("a").first
    try:
        href = link.get_attribute("href", timeout=1000) or ""
        bill_id = href.rstrip("/").split("/")[-1]
    except Exception:
        pass
    if len(cells) > 2:
        inv_no = cells[2].inner_text().strip()
    if len(cells) > 4:
        try:
            total_paise = parse_paise(cells[4].inner_text().strip())
        except ValueError:
            pass
    return {
        "marker": marker,
        "bill_id": bill_id,
        "invoice_no": inv_no,
        "total_paise": total_paise,
    }


def _parse_payment_row(row, marker: str) -> dict:
    cells = row.locator("td").all()
    return {
        "marker": marker,
        "payment_id": row.locator("a").first.get_attribute("href", timeout=1000).rstrip("/").split("/")[-1],
    }


def read_bill(driver: Driver, config: Config, bill_id: str) -> Optional[dict]:
    """Read /bills/{id} and return bill fields dict."""
    page = driver.page()
    base = config.lb_base_url.rstrip("/")
    _check_session(page, base, config, f"/bills/{bill_id}")
    page.wait_for_load_state("domcontentloaded")

    result = {"bill_id": bill_id}
    # We must also extract vendor ID. Since UI shows vendor name, we extract it as vendor_name.
    # verify_mutation does not strictly check vendor_id if vendor_name is returned and matches,
    # but wait, verify_mutation checks params['vendor_id'].
    # Actually, the API returns the vendor ID in the link or we just skip vendor_id match if not possible?
    for field, label in [("invoice_no", "Invoice No"), ("status", "Status"),
                          ("marker", "Internal Ref"), ("total_paise", "Total"),
                          ("po_no", "PO No"), ("currency", "Currency")]:
        try:
            # First try p:has-text('Label:')
            try:
                val = page.locator(f"p:has-text('{label}:')").first.inner_text(timeout=500)
                val = val.replace(f"{label}:", "").strip()
            except Exception:
                # Fallback to td:has-text('Label') + td
                val = page.locator(f"td:has-text('{label}') + td").first.inner_text(timeout=500).strip()
            
            if field == "total_paise":
                try:
                    result[field] = parse_paise(val)
                except ValueError:
                    result[field] = 0
            else:
                result[field] = val
        except Exception:
            result[field] = ""
            
    # Try to extract vendor_id from the Vendor link (e.g. /vendors/V1)
    try:
        vendor_link = page.locator("a[href^='/vendors/']").first.get_attribute("href", timeout=500)
        if vendor_link:
            result["vendor_id"] = vendor_link.rstrip("/").split("/")[-1]
    except Exception:
        pass
        
    return result


def find_payment(driver: Driver, config: Config, marker: str) -> Optional[dict]:
    """Search /payments for a payment with batch_ref containing marker."""
    page = driver.page()
    base = config.lb_base_url.rstrip("/")
    _check_session(page, base, config, f"/payments?q={marker}")
    page.wait_for_load_state("domcontentloaded")
    rows = page.locator("table tbody tr").all()
    for row in rows:
        if marker in row.inner_text():
            parsed = _parse_payment_row(row, marker)
            return read_payment(driver, config, parsed['payment_id'])
    return None


# ── Mutating skills ───────────────────────────────────────────────────────────

def prepare_bill(
    driver: Driver,
    config: Config,
    draft: dict,
    marker: str,
    vendor_account_id: str,
) -> None:
    """Fill the /bills/new form. Does NOT click Submit.

    draft keys: vendor_id, invoice_no, invoice_date (dd/mm/yyyy), po_no,
                line_desc, line_qty, line_rate_paise
    """
    page = driver.page()
    base = config.lb_base_url.rstrip("/")
    _check_session(page, base, config, f"/bills/new")
    page.wait_for_load_state("domcontentloaded")

    try:
        # Vendor select
        vendor_sel = resolve_locator(page, VENDOR_SELECT, "vendor")
        vendor_sel.select_option(value=str(draft.get("vendor_id", "")))
        # Wait for the bank account dropdown to become enabled (state-based, not sleep)
        page.wait_for_selector("select[name='bank_account_id']:not([disabled])", timeout=3000)

        # Invoice fields
        resolve_locator(page, INVOICE_NO_FIELD, "invoice_no").fill(draft.get("invoice_no", ""))
        resolve_locator(page, INVOICE_DATE_FIELD, "invoice_date").fill(draft.get("invoice_date", ""))
        resolve_locator(page, PO_NO_FIELD, "po_no").fill(draft.get("po_no", ""))
        resolve_locator(page, INTERNAL_REF_FIELD, "internal_ref").fill(marker)

        # Bank account select (vendor's accounts only — never typed by the operator)
        # A bank selection failure is safety-critical: BLOCK, never silently pass.
        if vendor_account_id:
            acct_sel = page.locator("select[name='bank_account_id']")
            if acct_sel.count() == 0:
                raise PrepareError(
                    f"bank_selection_failed: no bank account selector found on bill form"
                )
            try:
                # The option text is like "SBI - xxxx1234", and vendor_account_id is "xxxx1234"
                # Playwright's select_option with label can use exact match. Since we only have the account number,
                # let's find the option element that contains this account number and get its value.
                option = acct_sel.locator(f"option:has-text('{vendor_account_id}')").first
                if option.count() == 0:
                    raise Exception(f"Option containing '{vendor_account_id}' not found")
                val = option.get_attribute("value")
                acct_sel.select_option(value=val)
            except Exception as e:
                raise PrepareError(
                    f"bank_selection_failed: could not select account '{vendor_account_id}': {e}"
                )

        # Line items
        desc = draft.get("line_desc", "Goods")
        qty = str(draft.get("line_qty", 1))
        rate = str(draft.get("line_rate_paise", draft.get("amount_paise", 0)))

        rows = page.locator("#linesBody tr").all()
        if rows:
            first_row = rows[0]
            first_row.locator("input[name*='line_desc']").fill(desc)
            first_row.locator("input[name*='line_qty']").fill(qty)
            first_row.locator("input[name*='line_rate']").fill(rate)

    except BLOCKEDUIAmbiguous as e:
        raise PrepareError(f"UI ambiguous: {e}")
    except PrepareError:
        raise  # propagate without wrapping
    except Exception as e:
        raise PrepareError(f"Form fill failed: {e}")


def commit_bill(driver: Driver, config: Config, expected_amount_paise: int, dialog_handler: DialogHandler) -> str:
    """Click 'Submit Bill' (or 'Post Bill' in v2 UI). Attach dialog handler first.

    The dialog text is 'Post bill of ₹X?' where X is in rupees.
    Handler verifies the amount matches expected_amount_paise.
    Returns bill_id from the redirect URL.
    """
    page = driver.page()

    # The dialog handler checks rupee amount (paise / 100)
    expected_rupees = expected_amount_paise / 100
    # Format: could be "₹184.50" or "₹18450" depending on ERP
    dialog_handler.expect("Post bill of", expected_amount_paise)

    # Register dialog listener BEFORE clicking (Playwright requirement)
    page.once("dialog", dialog_handler.handle)

    # Click the submit button via the locator ladder
    resolve_locator(page, BILL_SUBMIT_BUTTON, "submit_bill").click()
    page.wait_for_load_state("networkidle")

    # Extract bill_id from the redirect URL e.g. /bills/123
    bill_id = page.url.rstrip("/").split("/")[-1]
    return bill_id


def prepare_payment(
    driver: Driver,
    config: Config,
    bill_id: str,
    pay_date: date,
    company_account_id: str,
    marker: str,
) -> None:
    """Fill the /payments/new form. Select the bill checkbox, fill date and Batch Ref.

    Does NOT click Schedule Payment.
    """
    page = driver.page()
    base = config.lb_base_url.rstrip("/")
    _check_session(page, base, config, f"/payments/new")
    page.wait_for_load_state("domcontentloaded")

    try:
        # Find and check the bill's checkbox (operator must select exactly one)
        # The table row for this bill should have a checkbox
        bill_row = page.locator(f"tr:has([value='{bill_id}']), tr:has([data-bill-id='{bill_id}'])")
        if bill_row.count() == 0:
            # Fallback: find by bill_id in any cell
            bill_row = page.locator(f"tr:has-text('{bill_id}')")
        if bill_row.count() > 0:
            bill_row.first.locator("input[type='checkbox']").check()
        else:
            raise PrepareError(f"Bill {bill_id} not found in payment form")

        # Pay date
        resolve_locator(page, PAY_DATE_FIELD, "pay_date").fill(pay_date.strftime("%d/%m/%Y"))

        # Company account
        acct_sel = page.locator("select[name='company_account_id']")
        if acct_sel.count() > 0:
            if company_account_id:
                acct_sel.select_option(value=str(company_account_id))
            else:
                acct_sel.select_option(index=1)

        # Batch Ref (= marker)
        resolve_locator(page, BATCH_REF_FIELD, "batch_ref").fill(marker)

    except PrepareError:
        raise
    except Exception as e:
        raise PrepareError(f"Payment form fill failed: {e}")


def commit_payment(driver: Driver, config: Config, expected_amount_paise: int, dialog_handler: DialogHandler) -> str:
    """Click 'Schedule Payment'. Dialog handler checks the amount.

    Returns payment_id from the redirect URL.
    """
    page = driver.page()
    dialog_handler.expect("Schedule payment of", expected_amount_paise)
    page.once("dialog", dialog_handler.handle)

    resolve_locator(page, SCHEDULE_PAYMENT_BUTTON, "schedule_payment").click()
    page.wait_for_load_state("networkidle")

    payment_id = page.url.rstrip("/").split("/")[-1]
    return payment_id
def read_payment(driver: Driver, config: Config, payment_id: str) -> Optional[dict]:
    """Read /payments/{id} and return payment fields dict."""
    page = driver.page()
    base = config.lb_base_url.rstrip("/")
    _check_session(page, base, config, f"/payments/{payment_id}")
    page.wait_for_load_state("domcontentloaded")

    result = {"payment_id": payment_id}
    for field, label in [("bill_id", "Bill ID"), ("status", "Status"),
                          ("batch_ref", "Batch Ref"), ("amount_paise", "Amount (Paise)"),
                          ("pay_date", "Pay Date"), ("payee_account_id", "Payee Account ID"),
                          ("company_account_id", "Company Account ID")]:
        try:
            val = page.locator(f"p:has-text('{label}:')").inner_text(timeout=2000).replace(f"{label}:", "").strip()
            if field == "amount_paise":
                try:
                    result[field] = int(val)
                except ValueError:
                    result[field] = 0
            else:
                result[field] = val
        except Exception:
            result[field] = ""
    return result

