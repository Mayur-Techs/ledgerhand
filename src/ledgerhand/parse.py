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
    """Parse a date string into a date object.

    Handles:
      - DD/MM/YYYY  (DMY) or MM/DD/YYYY (MDY)  — separators: / - . space
      - DD-Mon-YYYY / DD Month YYYY  e.g. '10-Sep-2026', '10 September 2026'
      - Mon DD YYYY                 e.g. 'Sep 10 2026'
    Raises DateParseError when the date cannot be parsed or is invalid.
    """
    MONTHS = {
        'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,
        'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12,
        'january':1,'february':2,'march':3,'april':4,'june':6,'july':7,
        'august':8,'september':9,'october':10,'november':11,'december':12,
    }
    parts = re.split(r'[-/.\s]+', text.strip())
    if len(parts) >= 3:
        # ── Month-name path ──────────────────────────────────────────────────
        for i, p in enumerate(parts):
            lo = p.lower()
            if lo in MONTHS:
                month = MONTHS[lo]
                others = [parts[j] for j in range(len(parts)) if j != i]
                try:
                    nums = [int(x) for x in others if x.lstrip('-').isdigit()]
                    if len(nums) == 2:
                        a, b = nums[0], nums[1]
                        # Larger number is year if > 31
                        if a > 31:
                            y, day = a, b
                        elif b > 31:
                            y, day = b, a
                        else:
                            # fallback: second is year (e.g. "10 Sep 26")
                            y, day = b, a
                        if y < 100:
                            y += 2000
                        return date(y, month, day)
                except (ValueError, TypeError):
                    pass
        # ── All-numeric path ─────────────────────────────────────────────────
        try:
            p1, p2, p3 = int(parts[0]), int(parts[1]), int(parts[2])
            # Detect ISO format YYYY-MM-DD (first part > 1000)
            if p1 > 1000:
                return date(p1, p2, p3)
            if fmt == 'DMY':
                day, m, y = p1, p2, p3
            else:
                m, day, y = p1, p2, p3
            if len(str(y)) == 2:
                y += 2000
            if day > 12 and m > 12:
                raise DateParseError("Ambiguous or invalid date")
            return date(y, m, day)
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
    return f"{kind}:{vendor_id}|{invoice_no_normalized}"

def make_marker(bk: str) -> str:
    return "LH-" + hashlib.sha256(bk.encode()).hexdigest()[:12]

def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(',', ':'))

def hash_params(params: dict) -> str:
    return hashlib.sha256(canonical_json(params).encode()).hexdigest()[:16]

def operation_key(kind: str, bk: str, params_hash: str) -> str:
    return f"{kind}:{bk}:{params_hash}"

def action_hash(payload: dict) -> str:
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()