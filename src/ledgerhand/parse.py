"""Parse verbatim text spans from LLM extraction into typed values."""

import hashlib
import json
import re
from datetime import date, timedelta

class DateParseError(Exception):
    pass

def normalize_invoice_no(raw: str) -> str:
    """upper(), drop non-alphanumerics, drop leading zeros of numeric tail"""
    s = re.sub(r'[^A-Z0-9]', '', raw.upper())
    # find the numeric tail
    match = re.search(r'([A-Z]*)0*([0-9]+)$', s)
    if match:
        return match.group(1) + match.group(2)
    return s

def parse_date(text: str, fmt: str = 'DMY') -> date:
    """parse dd/mm/yyyy (DMY) or mm/dd/yyyy (MDY)"""
    parts = re.split(r'[-/.\s]+', text)
    if len(parts) >= 3:
        try:
            p1, p2, p3 = int(parts[0]), int(parts[1]), int(parts[2])
            if fmt == 'DMY':
                d, m, y = p1, p2, p3
            else:
                m, d, y = p1, p2, p3
            
            if len(str(y)) == 2:
                y += 2000
                
            if d > 12 and m > 12:
                raise DateParseError("Ambiguous or invalid date")
                
            return date(y, m, d)
        except ValueError:
            pass
    raise DateParseError(f"Could not parse date: {text}")

def next_business_day(d: date) -> date:
    """skip weekends"""
    d += timedelta(days=1)
    while d.weekday() >= 5: # 5=Sat, 6=Sun
        d += timedelta(days=1)
    return d

def edit_distance(a: str, b: str) -> int:
    """Levenshtein distance"""
    if len(a) < len(b):
        return edit_distance(b, a)
    if len(b) == 0:
        return len(a)
    previous_row = range(len(b) + 1)
    for i, c1 in enumerate(a):
        current_row = [i + 1]
        for j, c2 in enumerate(b):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    return previous_row[-1]

def business_key(kind: str, vendor_id: str, invoice_no_normalized: str) -> str:
    """bill: "bill:" + vendor_id + "|" + normalized
    payment: "pay:" + business_key(bill, ...)"""
    if kind == "bill":
        return f"bill:{vendor_id}|{invoice_no_normalized}"
    elif kind == "payment":
        return f"pay:bill:{vendor_id}|{invoice_no_normalized}"
    return f"{kind}:{vendor_id}|{invoice_no_normalized}"

def make_marker(bk: str) -> str:
    """LH- + sha256(bk)[:12]"""
    return "LH-" + hashlib.sha256(bk.encode()).hexdigest()[:12]

from pydantic import BaseModel

def _json_default(obj):
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, date):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

def canonical_json(obj) -> str:
    return json.dumps(obj, default=_json_default, separators=(',', ':'), sort_keys=True)

def hash_params(params: dict) -> str:
    """sha256 of canonical_json(params)"""
    return hashlib.sha256(canonical_json(params).encode()).hexdigest()

def operation_key(kind: str, bk: str, params_hash: str) -> str:
    return hashlib.sha256(f"{kind}{bk}{params_hash}".encode()).hexdigest()

def action_hash(payload: dict) -> str:
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()