import sqlite3, json
conn = sqlite3.connect('runs/ledger.db')
conn.row_factory = sqlite3.Row
runs = conn.execute("SELECT id, status, verdict FROM runs ORDER BY started_at DESC LIMIT 5").fetchall()
print("Recent runs:")
for r in runs:
    print(f"  {r['id']} | {r['status']} | {r['verdict']}")

print()
tasks = conn.execute("SELECT file_path, status, reason_code, decision_json FROM tasks ORDER BY rowid").fetchall()
print(f"Total tasks: {len(tasks)}")
for t in tasks:
    dec = {}
    if t["decision_json"]:
        try:
            dec = json.loads(t["decision_json"])
        except:
            pass
    fname = t["file_path"].split("/")[-1].split("\\")[-1] if t["file_path"] else "?"
    print(f"  {fname:20s} | {str(t['status']):12s} | bill:{dec.get('bill_decision',''):12s} pay:{dec.get('pay_decision',''):8s} | {str(t['reason_code'] or ''):30s}")

print()
events = conn.execute("SELECT kind, task_id, payload_json FROM events ORDER BY seq DESC LIMIT 20").fetchall()
print("Last 20 events:")
for e in events:
    print(f"  {e['kind']:30s} | task={e['task_id'] or '-'}")
