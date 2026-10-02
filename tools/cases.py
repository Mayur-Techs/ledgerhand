"""Canonical definitions for Vendors, POs, and test cases.

Used by both the PDF generator (make_data.py) and the mock ERP (seed.py)
so the external world and internal application perfectly align.
"""

VENDORS = [
    ("V1", "Sharma Traders", "27AABCS1234A1Z5"),
    ("V2", "Kaveri Foods Pvt Ltd", "29AABCK5678B1Z3"),
    ("V3", "Nimbus Logistics LLP", "07AABCN9012C1Z1"),
    ("V4", "Orbit Stationers", "27AABCO3456D1Z9"),
    ("V5", "BlueLeaf Packaging", "33AABCB7890E1Z7"),
    ("V6", "Deccan IT Services", "36AABCD2345F1Z5"),
    ("V8", "Global Cargo Inc", "27AABCG6789G1Z3"),
]

POS = [
    ("PO-2026-001", "V1", "Open"),
    ("PO-2026-002", "V2", "Open"),
    ("PO-2026-003", "V3", "Open"),
    ("PO-2025-004", "V4", "Open"),
    ("PO-2026-006", "V1", "Open"),
    ("PO-2026-007", "V2", "Open"),
    ("PO-2026-008", "V5", "Open"),
    ("PO-2026-009", "V6", "Open"),
    ("PO-2026-010", "V6", "Open"),
]

PO_LINES = [
    ("PO-2026-001", "Goods", 1, 1, 1845000),
    ("PO-2026-002", "Goods", 50, 50, 84600),
    ("PO-2026-003", "Service", 1, 1, 7200000),
    ("PO-2025-004", "Supplies", 1, 1, 1500000),
    ("PO-2026-006", "Goods", 1, 1, 4000000), # Price mismatch intentionally
    ("PO-2026-007", "Goods", 1, 0, 5000000), # Receipt mismatch intentionally
    ("PO-2026-008", "Goods", 1, 1, 2800000),
    ("PO-2026-009", "Service", 1, 1, 3500000),
    ("PO-2026-010", "Service", 1, 1, 3300000),
]
