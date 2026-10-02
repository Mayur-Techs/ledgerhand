"""Invoice extractor — PDF text → InvoiceDraft → ParsedInvoice.

Why: extraction is split from parsing. The model returns verbatim text
spans; code does all arithmetic, normalisation and type conversion in
parse.py. Low confidence on critical fields → hold, never guess.
"""
from __future__ import annotations
import hashlib
from pathlib import Path
from typing import Optional

from .models import InvoiceDraft, ParsedInvoice, InvoiceLine
from .config import Config
from .llm.prompts import EXTRACT_INVOICE_PROMPT, PROMPT_VERSION_EXTRACT
from .llm.client import complete_json
from .parse import normalize_invoice_no, parse_date, DateParseError
from .money import parse_paise
from .checks import CheckResult


def extract_pdf(pdf_path: Path, config: Config, llm_mode: str = "replay",
                ledger=None) -> tuple[str, InvoiceDraft]:
    """Extract invoice data from a PDF file.
    
    Returns (file_sha256, InvoiceDraft).
    Checks ledger extraction cache first.
    """
    file_bytes = pdf_path.read_bytes()
    file_sha256 = hashlib.sha256(file_bytes).hexdigest()
    
    # Check extraction cache
    if ledger is not None:
        cached = ledger.get_cached_extraction(file_sha256, PROMPT_VERSION_EXTRACT)
        if cached:
            import json
            return file_sha256, InvoiceDraft(**json.loads(cached))
    
    # Extract text from PDF
    invoice_text = _extract_text(pdf_path)
    
    # Call LLM for extraction (verbatim spans only)
    prompt = EXTRACT_INVOICE_PROMPT.format(invoice_text=invoice_text)
    raw = complete_json(
        prompt=prompt,
        schema_name="InvoiceDraft",
        prompt_version=PROMPT_VERSION_EXTRACT,
        input_text=invoice_text,
        model=config.llm_model,
        llm_mode=llm_mode,
    )
    
    draft = InvoiceDraft(**{k: raw.get(k, v) for k, v in InvoiceDraft().model_dump().items()})
    draft.raw_text = invoice_text
    
    # Cache the extraction
    if ledger is not None:
        import json
        ledger.cache_extraction(file_sha256, PROMPT_VERSION_EXTRACT, json.dumps(draft.model_dump()), config.llm_model)
    
    return file_sha256, draft


def _extract_text(pdf_path: Path) -> str:
    """Extract text from PDF using pdfplumber."""
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as e:
        raise ValueError(f"Cannot extract text from {pdf_path}: {e}")


def parse_draft(draft: InvoiceDraft, config: Config) -> tuple[Optional[ParsedInvoice], list[CheckResult]]:
    """Parse InvoiceDraft into ParsedInvoice. Code does all arithmetic.
    
    Returns (ParsedInvoice, []) on success.
    Returns (None, [CheckResult(fail)]) if a critical field cannot be parsed.
    """
    issues: list[CheckResult] = []
    
    # Check critical field confidence
    critical_fields = [
        ("vendor_gstin", draft.vendor_gstin_confidence),
        ("invoice_no", draft.invoice_no_confidence),
        ("total", draft.total_confidence),
        ("po_no", draft.po_no_confidence),
    ]
    low_conf = [
        f"{name}={conf:.2f}"
        for name, conf in critical_fields
        if conf < config.min_confidence_critical
    ]
    if low_conf:
        issues.append(CheckResult(
            check="confidence", result="fail",
            evidence=f"Low confidence on critical fields: {', '.join(low_conf)}",
        ))
        return None, issues
    
    # Parse total (code does arithmetic, never the model)
    try:
        total_paise = parse_paise(draft.total_text) if draft.total_text else 0
    except ValueError as e:
        issues.append(CheckResult(check="parse_total", result="fail", evidence=str(e)))
        return None, issues
    
    try:
        subtotal_paise = parse_paise(draft.subtotal_text) if draft.subtotal_text else total_paise
    except ValueError:
        subtotal_paise = total_paise
    
    try:
        tax_paise = parse_paise(draft.tax_text) if draft.tax_text else 0
    except ValueError:
        tax_paise = 0
    
    # Parse date
    invoice_date = None
    try:
        invoice_date = parse_date(draft.invoice_date_text, fmt=config.date_format)
    except (DateParseError, ValueError) as e:
        issues.append(CheckResult(check="date", result="fail", evidence=str(e)))
        return None, issues
    
    # Normalize invoice number
    invoice_no = normalize_invoice_no(draft.invoice_no_text) if draft.invoice_no_text else ""
    
    parsed = ParsedInvoice(
        vendor_gstin=draft.vendor_gstin_text.strip(),
        vendor_name=draft.vendor_name_text.strip(),
        invoice_no=invoice_no,
        invoice_no_raw=draft.invoice_no_text,
        invoice_date=invoice_date,
        po_no=draft.po_no_text.strip(),
        total_paise=total_paise,
        subtotal_paise=subtotal_paise,
        tax_paise=tax_paise,
        bank_account_claimed=draft.bank_account_text.strip(),
        currency=draft.currency_text.strip() or "INR",
    )
    return parsed, []