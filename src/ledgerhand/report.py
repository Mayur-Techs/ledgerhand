"""Report / Proof Pack generator.

Why: the Proof Pack is the independent evidence record that a human
reviewer or auditor can check without re-running the system. It proves
what happened, not just what we claim happened.

Contents of runs/<run_id>/:
  result.json      — machine-readable run verdict + stats
  audit.jsonl      — per-invariant I1–I8 results (one JSON object per line)
  summary.html     — human-readable HTML decision table
  held_items.csv   — all HOLDs with evidence for finance team
  screenshots/     — any screenshots captured during the run
  trace.zip        — Playwright browser trace (if tracing was enabled)
  proof_pack.zip   — all of the above in one archive for submission
"""
from __future__ import annotations
import csv
import io
import json
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .ledger import Ledger
from .audit import AuditResult


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def generate_proof_pack(
    run_id: str,
    ledger: Ledger,
    output_dir: Path,
    audit_result: Optional[AuditResult] = None,
    screenshot_dir: Optional[Path] = None,
    trace_zip: Optional[Path] = None,
) -> Path:
    """Generate the full Proof Pack for a completed run.

    Creates all output files under output_dir, then packages them into
    proof_pack.zip.  Returns the path to the ZIP.
    """
    from .audit import audit_run

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Run audit if not supplied
    if audit_result is None:
        audit_result = audit_run(run_id, ledger)

    run = ledger.get_run(run_id)
    tasks = ledger.get_tasks(run_id)

    # ── result.json ───────────────────────────────────────────────────────
    result_data = {
        "run_id": run_id,
        "goal_text": run.goal_text if run else "",
        "inbox": run.inbox if run else "",
        "llm_mode": run.llm_mode if run else "",
        "status": run.status if run else "",
        "verdict": run.verdict if run else audit_result.verdict,
        "started_at": run.started_at if run else "",
        "ended_at": run.ended_at if run else "",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": {
            "total_tasks": audit_result.total_tasks,
            "correct": audit_result.correct,
            "wrong": audit_result.wrong,
            "missing": audit_result.missing,
            "chain_valid": audit_result.chain_valid,
        },
        "invariants_passed": all(inv.passed for inv in audit_result.invariants),
        "tasks": [
            {
                "task_id": row.get("task_id"),
                "invoice_no": row.get("invoice_no"),
                "actual_bill": row.get("actual_bill_decision"),
                "actual_pay": row.get("actual_pay_decision"),
                "expected_bill": row.get("expected_bill_decision"),
                "expected_pay": row.get("expected_pay_decision"),
                "match": row.get("match"),
            }
            for row in audit_result.rows
        ],
    }
    (output_dir / "result.json").write_text(
        json.dumps(result_data, indent=2, default=str), encoding="utf-8"
    )

    # ── audit.jsonl ───────────────────────────────────────────────────────
    audit_lines = []
    for inv in audit_result.invariants:
        audit_lines.append(json.dumps({
            "invariant": inv.invariant,
            "passed": inv.passed,
            "evidence": inv.evidence,
        }))
    (output_dir / "audit.jsonl").write_text(
        "\n".join(audit_lines) + "\n", encoding="utf-8"
    )

    # ── summary.html ─────────────────────────────────────────────────────
    html = _build_summary_html(run_id, result_data, audit_result, tasks, ledger)
    (output_dir / "summary.html").write_text(html, encoding="utf-8")

    # ── held_items.csv ────────────────────────────────────────────────────
    held_csv = _build_held_items_csv(tasks)
    (output_dir / "held_items.csv").write_text(held_csv, encoding="utf-8")

    # ── screenshots/ ──────────────────────────────────────────────────────
    screenshots_dir = output_dir / "screenshots"
    screenshots_dir.mkdir(exist_ok=True)
    if screenshot_dir and screenshot_dir.exists():
        for png in screenshot_dir.glob("*.png"):
            shutil.copy(png, screenshots_dir / png.name)

    # ── trace.zip ─────────────────────────────────────────────────────────
    if trace_zip and trace_zip.exists():
        shutil.copy(trace_zip, output_dir / "trace.zip")

    # ── proof_pack.zip ────────────────────────────────────────────────────
    pack_path = output_dir / "proof_pack.zip"
    with zipfile.ZipFile(pack_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file in output_dir.iterdir():
            if file.name != "proof_pack.zip" and file.is_file():
                zf.write(file, file.name)
        if screenshots_dir.exists():
            for png in screenshots_dir.glob("*.png"):
                zf.write(png, f"screenshots/{png.name}")

    return pack_path


# ─────────────────────────────────────────────────────────────────────────────
# CSV helpers
# ─────────────────────────────────────────────────────────────────────────────

def generate_csv_report(run_id: str, ledger: Ledger) -> str:
    """Generate a CSV summary for spreadsheet review.

    Columns: file, vendor, invoice_no, bill_decision, pay_decision, reason_code
    """
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["file", "vendor", "invoice_no", "bill_decision", "pay_decision", "reason_code"])

    tasks = ledger.get_tasks(run_id)
    for task in tasks:
        file_name = Path(task.file_path).name if task.file_path else ""
        vendor = invoice_no = bill_decision = pay_decision = ""

        if task.invoice_json:
            inv = json.loads(task.invoice_json)
            vendor = inv.get("vendor_id", "")
            invoice_no = inv.get("invoice_no", "")

        if task.decision_json:
            dec = json.loads(task.decision_json)
            bill_decision = dec.get("bill_decision", "")
            pay_decision = dec.get("pay_decision", "")

        writer.writerow([file_name, vendor, invoice_no, bill_decision, pay_decision, task.reason_code or ""])

    return output.getvalue()


def _build_held_items_csv(tasks) -> str:
    """Build CSV of HOLDs with evidence for finance team review."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["file", "vendor", "invoice_no", "reason_code", "evidence_summary"])

    for task in tasks:
        decision_json = task.decision_json
        if not decision_json:
            continue
        dec = json.loads(decision_json)
        if dec.get("bill_decision") not in ("HOLD", "QUARANTINE"):
            continue

        file_name = Path(task.file_path).name if task.file_path else ""
        vendor = invoice_no = ""
        if task.invoice_json:
            inv = json.loads(task.invoice_json)
            vendor = inv.get("vendor_id", "")
            invoice_no = inv.get("invoice_no", "")

        # Collect evidence from checks
        evidence_parts = []
        if task.checks_json:
            checks = json.loads(task.checks_json)
            for check in checks:
                if check.get("result") in ("fail", "warn"):
                    evidence_parts.append(check.get("evidence", ""))
        evidence = " | ".join(filter(None, evidence_parts))

        writer.writerow([file_name, vendor, invoice_no, task.reason_code or "", evidence])

    return output.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# HTML summary
# ─────────────────────────────────────────────────────────────────────────────

def _build_summary_html(
    run_id: str,
    result_data: dict,
    audit_result: AuditResult,
    tasks,
    ledger: Ledger,
) -> str:
    """Build a self-contained HTML summary page."""
    verdict_color = {"PASS": "#2d8a2d", "FAIL": "#c0392b", "PARTIAL": "#e67e22"}.get(
        audit_result.verdict, "#555"
    )

    inv_rows = "".join(
        f"<tr><td>{'✓' if inv.passed else '✗'}</td>"
        f"<td><code>{_esc(inv.invariant)}</code></td>"
        f"<td>{_esc(inv.evidence)}</td></tr>"
        for inv in audit_result.invariants
    )

    task_rows = ""
    for row in audit_result.rows:
        match_cell = (
            '<td style="color:green">✓</td>' if row.get("match")
            else '<td style="color:red">✗</td>'
        )
        task_rows += (
            f"<tr>"
            f"<td>{_esc(row.get('invoice_no',''))}</td>"
            f"<td>{_esc(row.get('actual_bill_decision',''))}</td>"
            f"<td>{_esc(row.get('actual_pay_decision',''))}</td>"
            f"<td>{_esc(row.get('expected_bill_decision','') or '')}</td>"
            f"{match_cell}"
            f"</tr>"
        )

    stats = result_data.get("stats", {})

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Ledgerhand Proof Pack — {_esc(run_id)}</title>
<style>
  body {{font-family: monospace; margin: 2em; background: #fafafa;}}
  h1 {{color: {verdict_color};}}
  table {{border-collapse: collapse; width:100%; margin-bottom:1.5em;}}
  th, td {{border: 1px solid #ccc; padding: 4px 8px; text-align:left;}}
  th {{background:#eee;}}
  .verdict {{font-size:1.4em; font-weight:bold; color:{verdict_color};}}
</style>
</head>
<body>
<h1>Ledgerhand Proof Pack</h1>
<p><strong>Run ID:</strong> {_esc(run_id)}</p>
<p><strong>Goal:</strong> {_esc(result_data.get('goal_text',''))}</p>
<p><strong>Inbox:</strong> {_esc(result_data.get('inbox',''))}</p>
<p><strong>Started:</strong> {_esc(result_data.get('started_at',''))}
   &nbsp; <strong>Ended:</strong> {_esc(result_data.get('ended_at',''))}</p>
<p class="verdict">Verdict: {_esc(audit_result.verdict)}</p>

<h2>Statistics</h2>
<table>
  <tr><th>Total tasks</th><td>{stats.get('total_tasks',0)}</td></tr>
  <tr><th>Correct</th><td>{stats.get('correct',0)}</td></tr>
  <tr><th>Wrong</th><td>{stats.get('wrong',0)}</td></tr>
  <tr><th>Missing</th><td>{stats.get('missing',0)}</td></tr>
  <tr><th>Hash chain valid</th><td>{'Yes' if stats.get('chain_valid') else 'NO — TAMPERED'}</td></tr>
</table>

<h2>Safety Invariants (I1–I8)</h2>
<table>
  <tr><th></th><th>Invariant</th><th>Evidence</th></tr>
  {inv_rows}
</table>

<h2>Per-invoice decisions</h2>
<table>
  <tr><th>Invoice No</th><th>Bill decision</th><th>Pay decision</th><th>Expected bill</th><th>Match</th></tr>
  {task_rows}
</table>

<p><em>Generated {_esc(result_data.get('generated_at',''))} by Ledgerhand {_esc(run_id[:8])}</em></p>
</body></html>"""


def _esc(s: str) -> str:
    """Minimal HTML escaping — no external dependency."""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )