from .db import get_db, init_db
from datetime import datetime, timezone

def seed_data():
    conn = get_db()
    with conn:
        # Company Accounts
        conn.executemany("INSERT INTO company_accounts (id, bank_name, account_number) VALUES (?, ?, ?)", [
            ("CA1", "HDFC Bank", "HDFC0001111"),
            ("CA2", "State Bank of India", "SBI0002222")
        ])

        # Vendors
        vendors = [
            ("V1", "Sharma Traders", "27AABCS1234A1Z5"),
            ("V2", "Kaveri Foods Pvt Ltd", "29AABCK5678B1Z3"),
            ("V3", "Nimbus Logistics LLP", "07AABCN9012C1Z1"),
            ("V4", "Orbit Stationers", "27AABCO3456D1Z9"),
            ("V5", "BlueLeaf Packaging", "33AABCB7890E1Z7"),
            ("V6", "Deccan IT Services", "36AABCD2345F1Z5"),
            ("V8", "Global Cargo Inc", "27AABCG6789G1Z3")
        ]
        conn.executemany("INSERT INTO vendors (id, name, gstin) VALUES (?, ?, ?)", vendors)

        # Vendor Accounts
        vendor_accounts = [
            ("VA1", "V1", "SBI", "xxxx1234", 1),
            ("VA2", "V2", "HDFC", "xxxx5678", 1),
            ("VA3", "V3", "ICICI", "xxxx9012", 1),
            ("VA4", "V4", "Axis", "xxxx3456", 1),
            ("VA5", "V5", "PNB", "xxxx7890", 1),
            ("VA6", "V6", "Kotak", "xxxx2345", 1),
            ("VA8", "V8", "Citi", "xxxx6789", 1)
        ]
        conn.executemany("INSERT INTO vendor_accounts (id, vendor_id, bank_name, account_number, is_default) VALUES (?, ?, ?, ?, ?)", vendor_accounts)

        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent.parent))
        from tools.cases import POS, PO_LINES
        
        # POs
        conn.executemany("INSERT INTO pos (po_no, vendor_id, status) VALUES (?, ?, ?)", POS)

        # PO Lines
        conn.executemany("INSERT INTO po_lines (po_no, item, ordered_qty, received_qty, rate_paise) VALUES (?, ?, ?, ?, ?)", PO_LINES)

        # Bills
        bills = [
            ("B1", "V1", "INV-001", "01/09/2026", 500000, "PO-2026-001", "INR", "IR-001", "Paid", datetime.now(timezone.utc).isoformat()),
            ("B2", "V2", "INV-002", "02/09/2026", 500000, "PO-2026-002", "INR", "IR-002", "Posted", datetime.now(timezone.utc).isoformat()),
            # A04 exact duplicate setup: V4, INV-2025-DUP, 1500000
            ("B3", "V4", "INV-2025-DUP", "05/09/2026", 1500000, "PO-2025-004", "INR", "IR-DUP", "Posted", datetime.now(timezone.utc).isoformat()),
        ]
        # Pad with extra bills to ensure B3 is not the only one on page 1
        for i in range(4, 15):
            bills.append((f"B{i}", "V3", f"INV-PAD-{i}", "01/09/2026", 10000, "PO-2026-003", "INR", f"IR-00{i}", "Posted", datetime.now(timezone.utc).isoformat()))

        conn.executemany("INSERT INTO bills (id, vendor_id, invoice_no, invoice_date, total_paise, po_no, currency, internal_ref, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", bills)

        # Payments
        conn.execute("INSERT INTO payments (id, bill_id, amount_paise, pay_date, payee_account_id, company_account_id, batch_ref, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", 
            ("PAY1", "B1", 500000, "05/09/2026", "VA1", "CA1", "BR-001", "Completed")
        )

if __name__ == "__main__":
    import os
    enforce_unique = os.environ.get("LB_ENFORCE_UNIQUE", "1") == "1"
    init_db(enforce_unique)
    seed_data()
    print("Database seeded.")