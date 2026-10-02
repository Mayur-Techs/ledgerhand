"""Money utilities — all amounts are integer paise.

Why: floating-point arithmetic causes silent rounding errors in financial
calculations. Keeping everything in integer paise makes every operation
exact and every bug loud.
"""
from __future__ import annotations
import re


RUPEES_SYMBOLS = ("₹", "Rs.", "Rs", "INR")


def parse_paise(text: str) -> int:
    """Parse a money string like '₹1,23,456.00' or 'Rs. 4,500' to integer paise.

    Raises ValueError if the string cannot be parsed unambiguously.
    The model never does this — code does.
    """
    s = text.strip()
    for sym in RUPEES_SYMBOLS:
        s = s.replace(sym, "")
    s = s.strip().replace(",", "").replace(" ", "")
    if not re.fullmatch(r"\d+(\.\d{1,2})?", s):
        raise ValueError(f"Cannot parse as money: {text!r}")
    if "." in s:
        rupees_s, paise_s = s.split(".")
        paise_part = int(paise_s.ljust(2, "0")[:2])
    else:
        rupees_s = s
        paise_part = 0
    return int(rupees_s) * 100 + paise_part


def paise_to_rupees_str(paise: int) -> str:
    """Format integer paise as '₹1,23,456.00' with Indian grouping."""
    if paise < 0:
        return "-" + paise_to_rupees_str(-paise)
    rupees, remainder = divmod(paise, 100)
    rs = str(rupees)
    # Indian grouping: last 3 digits, then groups of 2
    if len(rs) > 3:
        grouped = rs[-3:]
        rs = rs[:-3]
        while rs:
            grouped = rs[-2:] + "," + grouped
            rs = rs[:-2]
        rs = grouped
    return f"₹{rs}.{remainder:02d}"


def add_paise(a: int, b: int) -> int:
    """Add two paise amounts. Exists to make arithmetic explicit in call sites."""
    return a + b


def within_tolerance(billed: int, po_rate: int, pct: float) -> bool:
    """Return True if billed is within pct% above po_rate.

    Tolerance is one-sided (over-billing is the risk).
    """
    threshold = po_rate + int(po_rate * pct / 100)
    return billed <= threshold
