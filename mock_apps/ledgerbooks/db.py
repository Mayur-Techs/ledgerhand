import sqlite3
import os
from contextlib import contextmanager
from typing import Generator

DB_PATH = os.path.join(os.path.dirname(__file__), "erp.db")

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

@contextmanager
def get_db_context() -> Generator[sqlite3.Connection, None, None]:
    conn = get_db()
    try:
        yield conn
    finally:
        conn.close()

def init_db(enforce_unique: bool = True):
    conn = get_db()
    with conn:
        conn.execute("DROP TABLE IF EXISTS payments")
        conn.execute("DROP TABLE IF EXISTS bill_lines")
        conn.execute("DROP TABLE IF EXISTS bills")
        conn.execute("DROP TABLE IF EXISTS po_lines")
        conn.execute("DROP TABLE IF EXISTS pos")
        conn.execute("DROP TABLE IF EXISTS vendor_accounts")
        conn.execute("DROP TABLE IF EXISTS vendors")
        conn.execute("DROP TABLE IF EXISTS sessions")
        conn.execute("DROP TABLE IF EXISTS chaos_flags")
        conn.execute("DROP TABLE IF EXISTS company_accounts")

        conn.execute('''
            CREATE TABLE vendors (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                gstin TEXT NOT NULL
            )
        ''')

        conn.execute('''
            CREATE TABLE vendor_accounts (
                id TEXT PRIMARY KEY,
                vendor_id TEXT NOT NULL,
                bank_name TEXT NOT NULL,
                account_number TEXT NOT NULL,
                is_default INTEGER NOT NULL,
                FOREIGN KEY (vendor_id) REFERENCES vendors (id)
            )
        ''')

        conn.execute('''
            CREATE TABLE pos (
                po_no TEXT PRIMARY KEY,
                vendor_id TEXT NOT NULL,
                status TEXT NOT NULL,
                FOREIGN KEY (vendor_id) REFERENCES vendors (id)
            )
        ''')

        conn.execute('''
            CREATE TABLE po_lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                po_no TEXT NOT NULL,
                item TEXT NOT NULL,
                ordered_qty INTEGER NOT NULL,
                received_qty INTEGER NOT NULL,
                rate_paise INTEGER NOT NULL,
                FOREIGN KEY (po_no) REFERENCES pos (po_no)
            )
        ''')

        # Bills constraints
        unique_vendor_invoice = "UNIQUE(vendor_id, invoice_no)" if enforce_unique else ""
        unique_internal_ref = "UNIQUE(internal_ref)" if enforce_unique else ""
        
        constraints = []
        if unique_vendor_invoice: constraints.append(unique_vendor_invoice)
        if unique_internal_ref: constraints.append(unique_internal_ref)
        constraint_str = (", " + ", ".join(constraints)) if constraints else ""

        conn.execute(f'''
            CREATE TABLE bills (
                id TEXT PRIMARY KEY,
                vendor_id TEXT NOT NULL,
                invoice_no TEXT NOT NULL,
                invoice_date TEXT NOT NULL,
                total_paise INTEGER NOT NULL,
                po_no TEXT,
                currency TEXT NOT NULL,
                internal_ref TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (vendor_id) REFERENCES vendors (id)
                {constraint_str}
            )
        ''')

        conn.execute('''
            CREATE TABLE bill_lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bill_id TEXT NOT NULL,
                description TEXT NOT NULL,
                qty INTEGER NOT NULL,
                rate_paise INTEGER NOT NULL,
                amount_paise INTEGER NOT NULL,
                FOREIGN KEY (bill_id) REFERENCES bills (id)
            )
        ''')

        unique_payment_bill = "UNIQUE(bill_id)" if enforce_unique else ""
        unique_payment_batch = "UNIQUE(batch_ref)" if enforce_unique else ""
        
        p_constraints = []
        if unique_payment_bill: p_constraints.append(unique_payment_bill)
        if unique_payment_batch: p_constraints.append(unique_payment_batch)
        p_constraint_str = (", " + ", ".join(p_constraints)) if p_constraints else ""

        conn.execute(f'''
            CREATE TABLE payments (
                id TEXT PRIMARY KEY,
                bill_id TEXT NOT NULL,
                amount_paise INTEGER NOT NULL,
                pay_date TEXT NOT NULL,
                payee_account_id TEXT NOT NULL,
                company_account_id TEXT NOT NULL,
                batch_ref TEXT NOT NULL,
                status TEXT NOT NULL,
                FOREIGN KEY (bill_id) REFERENCES bills (id),
                FOREIGN KEY (payee_account_id) REFERENCES vendor_accounts (id)
                {p_constraint_str}
            )
        ''')

        conn.execute('''
            CREATE TABLE company_accounts (
                id TEXT PRIMARY KEY,
                bank_name TEXT NOT NULL,
                account_number TEXT NOT NULL
            )
        ''')

        conn.execute('''
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                request_count INTEGER NOT NULL DEFAULT 0
            )
        ''')

        conn.execute('''
            CREATE TABLE chaos_flags (
                name TEXT PRIMARY KEY,
                armed INTEGER NOT NULL DEFAULT 0,
                after_n_calls INTEGER NOT NULL DEFAULT 0,
                calls_remaining INTEGER NOT NULL DEFAULT 0,
                times INTEGER NOT NULL DEFAULT 0
            )
        ''')