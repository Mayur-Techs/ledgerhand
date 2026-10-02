import json
import pytest
from pathlib import Path
from ledgerhand.ledger import Ledger
from ledgerhand.audit import audit_run, AuditResult
from ledgerhand.report import generate_proof_pack, generate_csv_report

@pytest.fixture
def test_db(tmp_path):
    return tmp_path / "ledger.db"

@pytest.fixture
def ledger(test_db):
    l = Ledger(test_db)
    l.create_run("Goal", "", "batch_a", "dummy")
    # create_run generates an id. let's fetch it
    run_id = l.conn.execute("SELECT id FROM runs").fetchone()[0]
    return l, run_id

def test_audit_all_correct(ledger, tmp_path):
    l, run_id = ledger
    
    expected_file = tmp_path / "expected_a.json"
    with open(expected_file, "w") as f:
        json.dump([
            {"invoice_no": "INV1", "bill_decision": "ENTER", "pay_decision": "AUTO"},
            {"invoice_no": "INV2", "bill_decision": "SKIP", "pay_decision": "NONE"},
        ], f)
        
    l.conn.execute("INSERT INTO tasks (id, run_id, invoice_json, decision_json) VALUES (?, ?, ?, ?)",
                   ("t1", run_id, json.dumps({"invoice_no": "INV1"}), json.dumps({"bill_decision": "ENTER", "pay_decision": "AUTO"})))
    l.conn.execute("INSERT INTO tasks (id, run_id, invoice_json, decision_json) VALUES (?, ?, ?, ?)",
                   ("t2", run_id, json.dumps({"invoice_no": "INV2"}), json.dumps({"bill_decision": "SKIP", "pay_decision": "NONE"})))
                   
    l.log_event(run_id, "start", "t1", {})
    l.log_event(run_id, "start", "t2", {})
    
    result = audit_run(run_id, l, expected_file)
    assert result.verdict == "PASS"
    assert result.correct == 2
    assert result.wrong == 0
    assert result.missing == 0
    assert result.chain_valid == True

def test_audit_wrong_decision(ledger, tmp_path):
    l, run_id = ledger
    expected_file = tmp_path / "expected_a.json"
    with open(expected_file, "w") as f:
        json.dump([
            {"invoice_no": "INV1", "bill_decision": "ENTER", "pay_decision": "AUTO"},
        ], f)
        
    l.conn.execute("INSERT INTO tasks (id, run_id, invoice_json, decision_json) VALUES (?, ?, ?, ?)",
                   ("t1", run_id, json.dumps({"invoice_no": "INV1"}), json.dumps({"bill_decision": "SKIP", "pay_decision": "NONE"})))
                   
    l.log_event(run_id, "start", "t1", {})
    
    result = audit_run(run_id, l, expected_file)
    assert result.verdict == "FAIL"
    assert result.wrong == 1

def test_audit_chain_tamper(ledger, tmp_path):
    l, run_id = ledger
    
    l.log_event(run_id, "start", "t1", {})
    
    # Tamper the hash manually
    l.conn.execute("UPDATE events SET hash = 'tampered'")
    
    result = audit_run(run_id, l, None)
    assert result.chain_valid == False
    assert result.verdict == "FAIL"

def test_audit_missing_task(ledger, tmp_path):
    l, run_id = ledger
    expected_file = tmp_path / "expected_a.json"
    with open(expected_file, "w") as f:
        json.dump([
            {"invoice_no": "INV1", "bill_decision": "ENTER", "pay_decision": "AUTO"},
        ], f)
        
    l.log_event(run_id, "start", "t1", {})
    
    result = audit_run(run_id, l, expected_file)
    assert result.verdict == "FAIL"
    assert result.missing == 1

def test_csv_report(ledger):
    l, run_id = ledger
    l.conn.execute("INSERT INTO tasks (id, run_id, file_path, invoice_json, decision_json, reason_code) VALUES (?, ?, ?, ?, ?, ?)",
                   ("t1", run_id, "/tmp/inv.pdf", json.dumps({"invoice_no": "INV1", "vendor_id": "V1"}), json.dumps({"bill_decision": "ENTER", "pay_decision": "AUTO"}), "ok"))
                   
    csv_str = generate_csv_report(run_id, l)
    assert "inv.pdf" in csv_str
    assert "INV1" in csv_str
    assert "ENTER" in csv_str
    assert "ok" in csv_str
