import os
import uuid
from typing import Optional
from datetime import datetime, timezone
import time

from fastapi import FastAPI, Request, Form, Depends, HTTPException, status, Response, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel

from .db import get_db, init_db
from .chaos import set_fault, check_fault, increment_session_requests

app = FastAPI(title="LedgerBooks Mock ERP")

# We use absolute path for templates
templates_dir = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=templates_dir)

# Settings
ENFORCE_UNIQUE = os.environ.get("LB_ENFORCE_UNIQUE", "1") == "1"

@app.on_event("startup")
def startup_event():
    init_db(ENFORCE_UNIQUE)
    from .seed import seed_data
    try:
        seed_data()
    except Exception as e:
        print("Seed error (maybe already seeded):", e)

# ----------------
# Dependencies
# ----------------
def get_session(request: Request):
    session_id = request.cookies.get("session_id")
    if not session_id:
        return None
        
    conn = get_db()
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not row:
        return None
        
    # Chaos check
    if check_fault("session_expiry"):
        count = increment_session_requests(session_id)
        # If N calls reached, expire it
        row = conn.execute("SELECT after_n_calls FROM chaos_flags WHERE name='session_expiry'").fetchone()
        if row and count > row['after_n_calls']:
            return None
    
    return session_id

def require_auth(session_id: Optional[str] = Depends(get_session)):
    if not session_id:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return session_id

# ----------------
# Routes
# ----------------
@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    return templates.TemplateResponse(request, "login.html", {"request": request, "csrf_token": "fake_csrf"})

@app.post("/login")
def login_post(
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...)
):
    if username == os.environ.get("LB_USERNAME", "admin") and password == os.environ.get("LB_PASSWORD", "ledger123"):
        session_id = str(uuid.uuid4())
        conn = get_db()
        with conn:
            conn.execute("INSERT INTO sessions (id, created_at, request_count) VALUES (?, ?, ?)", 
                         (session_id, datetime.now(timezone.utc).isoformat(), 0))
        response = RedirectResponse(url="/bills", status_code=303)
        response.set_cookie("session_id", session_id)
        return response
    return RedirectResponse(url="/login?error=1", status_code=303)

@app.get("/logout")
def logout(response: Response):
    response.delete_cookie("session_id")
    return RedirectResponse(url="/login", status_code=303)

@app.get("/vendors", response_class=HTMLResponse)
def vendors_list(request: Request, _=Depends(require_auth)):
    conn = get_db()
    vendors = conn.execute("SELECT * FROM vendors").fetchall()
    return templates.TemplateResponse(request, "vendors.html", {"request": request, "vendors": vendors})

@app.get("/vendors/{id}", response_class=HTMLResponse)
def vendor_detail(request: Request, id: str, _=Depends(require_auth)):
    conn = get_db()
    vendor = conn.execute("SELECT * FROM vendors WHERE id=?", (id,)).fetchone()
    accounts = conn.execute("SELECT * FROM vendor_accounts WHERE vendor_id=?", (id,)).fetchall()
    return templates.TemplateResponse(request, "vendor_detail.html", {"request": request, "vendor": vendor, "accounts": accounts})

@app.get("/pos", response_class=HTMLResponse)
def pos_list(request: Request, _=Depends(require_auth)):
    conn = get_db()
    pos = conn.execute("SELECT * FROM pos").fetchall()
    return templates.TemplateResponse(request, "pos.html", {"request": request, "pos": pos})

@app.get("/pos/{po_no}", response_class=HTMLResponse)
def po_detail(request: Request, po_no: str, _=Depends(require_auth)):
    conn = get_db()
    po = conn.execute("SELECT * FROM pos WHERE po_no=?", (po_no,)).fetchone()
    lines = conn.execute("SELECT * FROM po_lines WHERE po_no=?", (po_no,)).fetchall()
    return templates.TemplateResponse(request, "po_detail.html", {"request": request, "po": po, "lines": lines})

@app.get("/bills", response_class=HTMLResponse)
def bills_list(request: Request, q: str = "", vendor: str = "", page: int = 1, _=Depends(require_auth)):
    conn = get_db()
    per_page = 10
    offset = (page - 1) * per_page
    
    query = """
        SELECT b.*, v.name as vendor_name 
        FROM bills b 
        JOIN vendors v ON b.vendor_id = v.id 
        WHERE 1=1
    """
    params = []
    
    if q:
        query += " AND (b.invoice_no LIKE ? OR b.internal_ref LIKE ?)"
        params.extend([f"%{q}%", f"%{q}%"])
    if vendor:
        query += " AND (v.name LIKE ? OR b.vendor_id = ?)"
        params.extend([f"%{vendor}%", vendor])
        
    query += " ORDER BY b.created_at DESC LIMIT ? OFFSET ?"
    params.extend([per_page, offset])
    
    bills = conn.execute(query, params).fetchall()
    
    # Count total
    count_query = "SELECT COUNT(*) as c FROM bills b JOIN vendors v ON b.vendor_id = v.id WHERE 1=1"
    if q:
        count_query += " AND (b.invoice_no LIKE ? OR b.internal_ref LIKE ?)"
    if vendor:
        count_query += " AND (v.name LIKE ? OR b.vendor_id = ?)"
    
    count_params = params[:-2]
    total = conn.execute(count_query, count_params).fetchone()['c']
    
    return templates.TemplateResponse(request, "bills.html", {
        "request": request, 
        "bills": bills, 
        "page": page,
        "total_pages": (total + per_page - 1) // per_page,
        "q": q,
        "vendor": vendor
    })

@app.get("/bills/new", response_class=HTMLResponse)
def bill_new_get(request: Request, _=Depends(require_auth)):
    conn = get_db()
    vendors = conn.execute("SELECT * FROM vendors").fetchall()
    accounts = conn.execute("SELECT * FROM vendor_accounts").fetchall()
    ui_variant = os.environ.get("LB_UI_VARIANT", "v1")
    if check_fault("ui_drift"):
        ui_variant = "v2"
        
    return templates.TemplateResponse(request, "bill_new.html", {
        "request": request, 
        "vendors": vendors, 
        "accounts": accounts,
        "ui_variant": ui_variant
    })

@app.post("/bills/new")
async def bill_new_post(request: Request, _=Depends(require_auth)):
    form = await request.form()
    
    vendor_id = form.get("vendor_id")
    invoice_no = form.get("invoice_no")
    invoice_date = form.get("invoice_date")
    po_no = form.get("po_no")
    currency = form.get("currency")
    internal_ref = form.get("internal_ref")
    action = form.get("action")
    
    # Parse lines (dynamic rows)
    lines = []
    i = 0
    total_paise = 0
    while f"line_desc_{i}" in form:
        desc = form.get(f"line_desc_{i}")
        qty = int(form.get(f"line_qty_{i}", 0))
        rate_paise = int(form.get(f"line_rate_{i}", 0))
        amount_paise = qty * rate_paise
        total_paise += amount_paise
        lines.append({
            "description": desc,
            "qty": qty,
            "rate_paise": rate_paise,
            "amount_paise": amount_paise
        })
        i += 1
        
    status = "Draft" if action == "Save Draft" else "Posted"
    bill_id = str(uuid.uuid4())
    
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO bills (id, vendor_id, invoice_no, invoice_date, total_paise, po_no, currency, internal_ref, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (bill_id, vendor_id, invoice_no, invoice_date, total_paise, po_no, currency, internal_ref, status, datetime.now(timezone.utc).isoformat()))
        
        for line in lines:
            conn.execute("""
                INSERT INTO bill_lines (bill_id, description, qty, rate_paise, amount_paise)
                VALUES (?, ?, ?, ?, ?)
            """, (bill_id, line["description"], line["qty"], line["rate_paise"], line["amount_paise"]))
        conn.commit()
    except sqlite3.IntegrityError as e:
        return HTMLResponse(f"Database Error: {e}", status_code=400)

    if check_fault("timeout_after_commit"):
        # Bill IS committed above. Delay just enough for Playwright's
        # page.goto() call to time out on this redirect response (~30s default),
        # but NOT so long that verify polling can't reach the ERP during recovery.
        # We raise 504 immediately (no sleep) so the Playwright request errors,
        # leaving intent=UNKNOWN. The verify loop then polls /bills?q=marker
        # which succeeds because the DB commit already happened.
        raise HTTPException(status_code=504, detail="Gateway Timeout (simulated)")

    return RedirectResponse(url=f"/bills/{bill_id}", status_code=303)

@app.get("/bills/{id}", response_class=HTMLResponse)
def bill_detail(request: Request, id: str, _=Depends(require_auth)):
    conn = get_db()
    bill = conn.execute("SELECT b.*, v.name as vendor_name FROM bills b JOIN vendors v ON b.vendor_id = v.id WHERE b.id=?", (id,)).fetchone()
    lines = conn.execute("SELECT * FROM bill_lines WHERE bill_id=?", (id,)).fetchall()
    return templates.TemplateResponse(request, "bill_detail.html", {"request": request, "bill": bill, "lines": lines})

@app.get("/payments/new", response_class=HTMLResponse)
def payment_new_get(request: Request, _=Depends(require_auth)):
    conn = get_db()
    bills = conn.execute("SELECT b.*, v.name as vendor_name FROM bills b JOIN vendors v ON b.vendor_id = v.id WHERE b.status='Posted'").fetchall()
    company_accounts = conn.execute("SELECT * FROM company_accounts").fetchall()
    vendor_accounts = conn.execute("SELECT * FROM vendor_accounts").fetchall()
    return templates.TemplateResponse(request, "payment_new.html", {
        "request": request, 
        "bills": bills, 
        "company_accounts": company_accounts,
        "vendor_accounts": vendor_accounts
    })

@app.post("/payments/new")
async def payment_new_post(request: Request, _=Depends(require_auth)):
    form = await request.form()
    
    bill_ids = form.getlist("bill_ids")
    if len(bill_ids) != 1:
        return HTMLResponse("Error: Select exactly one bill.", status_code=400)
        
    bill_id = bill_ids[0]
    pay_date = form.get("pay_date")
    company_account_id = form.get("company_account_id")
    batch_ref = form.get("batch_ref")
    
    conn = get_db()
    bill = conn.execute("SELECT * FROM bills WHERE id=?", (bill_id,)).fetchone()
    if not bill:
        return HTMLResponse("Bill not found.", status_code=404)
        
    # Get default vendor account
    v_acc = conn.execute("SELECT id FROM vendor_accounts WHERE vendor_id=? AND is_default=1", (bill['vendor_id'],)).fetchone()
    payee_account_id = v_acc['id'] if v_acc else ""
    
    payment_id = str(uuid.uuid4())
    
    try:
        conn.execute("""
            INSERT INTO payments (id, bill_id, amount_paise, pay_date, payee_account_id, company_account_id, batch_ref, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'Scheduled')
        """, (payment_id, bill_id, bill['total_paise'], pay_date, payee_account_id, company_account_id, batch_ref))
        
        conn.execute("UPDATE bills SET status='Scheduled' WHERE id=?", (bill_id,))
        conn.commit()
    except sqlite3.IntegrityError as e:
        return HTMLResponse(f"Database Error: {e}", status_code=400)

    if check_fault("timeout_after_commit"):
        time.sleep(30)
        raise HTTPException(status_code=504, detail="Gateway Timeout")

    return RedirectResponse(url=f"/payments/{payment_id}", status_code=303)

@app.get("/payments", response_class=HTMLResponse)
def payments_list(request: Request, _=Depends(require_auth)):
    conn = get_db()
    payments = conn.execute("""
        SELECT p.*, b.invoice_no, v.name as vendor_name 
        FROM payments p 
        JOIN bills b ON p.bill_id = b.id
        JOIN vendors v ON b.vendor_id = v.id
        ORDER BY p.pay_date DESC
    """).fetchall()
    return templates.TemplateResponse(request, "payments.html", {"request": request, "payments": payments})

@app.get("/payments/{id}", response_class=HTMLResponse)
def payment_detail(request: Request, id: str, _=Depends(require_auth)):
    conn = get_db()
    payment = conn.execute("""
        SELECT p.*, b.invoice_no, v.name as vendor_name 
        FROM payments p 
        JOIN bills b ON p.bill_id = b.id
        JOIN vendors v ON b.vendor_id = v.id
        WHERE p.id=?
    """, (id,)).fetchone()
    return templates.TemplateResponse(request, "payment_detail.html", {"request": request, "payment": payment})

@app.get("/admin/chaos", response_class=HTMLResponse)
def admin_chaos_get(request: Request):
    conn = get_db()
    flags = conn.execute("SELECT * FROM chaos_flags").fetchall()
    return templates.TemplateResponse(request, "admin_chaos.html", {"request": request, "flags": flags, "enforce_unique": ENFORCE_UNIQUE})

class ChaosArm(BaseModel):
    name: str
    after_n_calls: int = 0
    times: int = 0

@app.post("/admin/chaos")
def admin_chaos_post(payload: ChaosArm):
    set_fault(payload.name, True, payload.after_n_calls, payload.times)
    return {"status": "armed"}

@app.post("/admin/chaos/disarm")
def admin_chaos_disarm(payload: ChaosArm):
    set_fault(payload.name, False)
    return {"status": "disarmed"}

@app.get("/admin/state.json")
def admin_state_json():
    # Read-only export for tests/auditor
    conn = get_db()
    vendors = [dict(r) for r in conn.execute("SELECT * FROM vendors").fetchall()]
    bills = [dict(r) for r in conn.execute("SELECT * FROM bills").fetchall()]
    payments = [dict(r) for r in conn.execute("SELECT * FROM payments").fetchall()]
    
    return {
        "vendors": vendors,
        "bills": bills,
        "payments": payments
    }

import sqlite3