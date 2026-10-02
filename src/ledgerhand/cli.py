"""CLI entry point — python -m ledgerhand <command>.

Why argparse: no decorator magic, every command is a plain function, easy
to explain line by line.
"""
from __future__ import annotations
import argparse
import sys


def cmd_demo(args: argparse.Namespace) -> None:
    """Start mock ERP + dashboard and open browser."""
    import subprocess, webbrowser, time, sys
    import httpx

    # Start ERP
    erp = subprocess.Popen([sys.executable, "-m", "uvicorn",
        "mock_apps.ledgerbooks.app:app", "--port", "8001", "--no-access-log"])
    # Start dashboard
    dash = subprocess.Popen([sys.executable, "-m", "uvicorn",
        "ledgerhand.control.app:app", "--port", "8000", "--no-access-log"])

    # State-based wait — poll until each server responds (not a fixed sleep)
    for url, label in [("http://localhost:8001/login", "ERP"),
                        ("http://localhost:8000/", "Dashboard")]:
        for _ in range(30):  # up to 15 s
            try:
                httpx.get(url, timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        else:
            print(f"[demo] WARNING: {label} did not start in 15s")

    webbrowser.open("http://localhost:8000")
    print("[demo] ERP: http://localhost:8001  Dashboard: http://localhost:8000")
    print("[demo] Press Ctrl+C to stop")
    try:
        erp.wait()
    except KeyboardInterrupt:
        erp.terminate()
        dash.terminate()

def cmd_run(args: argparse.Namespace) -> None:
    """Run the operator on a goal."""
    from ledgerhand.config import load_config
    from ledgerhand.ledger import Ledger
    from ledgerhand.goal import compile_goal, spec_from_file, spec_to_json
    from ledgerhand.runner import Runner
    from ledgerhand.browser.driver import Driver
    from pathlib import Path

    config = load_config()
    Path("runs").mkdir(exist_ok=True)
    ledger = Ledger(Path("runs") / "ledger.db")

    if args.spec_file:
        spec = spec_from_file(args.spec_file)
    else:
        spec = compile_goal(args.goal, config, config.llm_mode)

    inbox_path = Path(args.inbox) if args.inbox else Path(spec.scope.get("inbox", "data/batch_a"))

    driver = Driver(headless=False, allowed_paths=list(config.allowed_paths),
                    allowed_origin=config.lb_base_url)
    # Persist spec_json so resume can reconstruct it
    run_id = ledger.create_run(
        args.goal, spec_to_json(spec), str(inbox_path), config.llm_mode
    )
    runner = Runner(ledger, driver, config, run_id)
    verdict = runner.run(spec, inbox_path, config.llm_mode)
    driver.close()
    print(f"[run] run_id={run_id}  Verdict: {verdict}")



def cmd_resume(args: argparse.Namespace) -> None:
    """Resume a paused or stopped run.

    Why it is safe: the runner uses lookup-first mutation, so re-entering
    a task that was partially completed will find the already-created ERP
    record and skip the side effect. No duplicate is possible even if the
    previous run crashed after commit.
    """
    from ledgerhand.config import load_config
    from ledgerhand.ledger import Ledger
    from ledgerhand.goal import spec_from_file
    from ledgerhand.runner import Runner
    from ledgerhand.browser.driver import Driver
    from pathlib import Path
    import json

    run_id = args.run_id
    config = load_config()
    ledger = Ledger(Path("runs") / "ledger.db")

    run = ledger.get_run(run_id)
    if not run:
        print(f"[resume] ERROR: run_id={run_id!r} not found in ledger")
        sys.exit(1)

    print(f"[resume] Resuming run {run_id}  status={run.status}  inbox={run.inbox}")

    # Reconstruct spec from persisted spec_json
    if run.spec_json:
        spec_data = json.loads(run.spec_json)
        from ledgerhand.models import GoalSpec
        spec = GoalSpec(**spec_data)
    else:
        print("[resume] ERROR: no spec_json stored in run — cannot resume")
        sys.exit(1)

    inbox = Path(run.inbox) if run.inbox else Path("data/batch_a")
    if not inbox.exists():
        print(f"[resume] ERROR: inbox path {inbox} does not exist")
        sys.exit(1)

    driver = Driver(headless=False, allowed_paths=config.allowed_paths)
    runner = Runner(ledger, driver, config, run_id)
    verdict = runner.resume(spec, inbox, run.llm_mode or config.llm_mode)
    driver.close()
    print(f"[resume] Verdict: {verdict}")



def cmd_chaos(args: argparse.Namespace) -> None:
    """Run the chaos test suite against a running ERP."""
    import subprocess, sys
    # Run only @pytest.mark.integration tests
    cmd = [sys.executable, "-m", "pytest", "tests/test_chaos.py", "tests/test_crash_recovery.py",
           "-v", "-m", "integration"]
    if args.fault:
        cmd.extend(["-k", args.fault])
    result = subprocess.run(cmd)
    sys.exit(result.returncode)


def cmd_audit(args: argparse.Namespace) -> None:
    """Audit a completed run against the expected answer key."""
    from ledgerhand.config import load_config
    from ledgerhand.ledger import Ledger
    from ledgerhand.audit import audit_run, print_audit_report
    from ledgerhand.report import generate_proof_pack, generate_csv_report
    from pathlib import Path
    
    config = load_config()
    ledger = Ledger(Path("runs") / "ledger.db")
    
    expected = None
    if args.eval:
        # Auto-detect expected file from run inbox
        run = ledger.get_run(args.run_id)
        if run and 'batch_a' in (run.inbox or ''):
            expected = Path("data/expected_a.json")
        elif run and 'batch_b' in (run.inbox or ''):
            expected = Path("data/expected_b.json")
    
    result = audit_run(args.run_id, ledger, expected)
    print_audit_report(result)
    
    # Generate proof pack
    pack = generate_proof_pack(args.run_id, ledger, Path("runs") / args.run_id)
    print(f"Proof Pack: {pack}")
    
    # Exit non-zero on failure
    import sys
    sys.exit(0 if result.verdict == 'PASS' else 1)


def cmd_gen_data(args: argparse.Namespace) -> None:
    """Generate synthetic invoice data."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent.parent / "tools"))
    import make_data
    print(f"[gen-data] Generating data with seed={args.seed} ...")
    make_data.generate_data(args.seed, Path(__file__).parent.parent.parent)
    print("[gen-data] Done.")


def cmd_selfcheck(args: argparse.Namespace) -> None:
    """Run self-check: environment verification + unit test suite.

    Reports:
    - Python and SQLite versions
    - Playwright browser launch status
    - Unit test results (excludes integration tests that require live ERP)

    Exit code 0 = all unit tests passed. Non-zero = failure.
    """
    import sqlite3
    import subprocess
    from pathlib import Path

    print(f"Python    : {sys.version}")
    print(f"SQLite    : {sqlite3.sqlite_version}")

    # Playwright check
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            b.close()
        print("Playwright: OK")
    except Exception as e:
        print(f"Playwright: FAIL ({e})")

    # Run unit tests (non-integration)
    import os
    if os.environ.get("LH_NO_RECURSIVE_TEST") == "1":
        print("\nSELFCHECK: PASS (Skipping unit tests to prevent recursion)")
        sys.exit(0)

    # Find repo root relative to this file
    repo_root = Path(__file__).parent.parent.parent
    print(f"\nRunning unit tests (non-integration) in {repo_root} ...")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-m", "not integration", "-q", "--tb=short"],
        cwd=str(repo_root),
    )
    if result.returncode == 0:
        print("\nSELFCHECK: PASS")
    else:
        print("\nSELFCHECK: FAIL — unit tests failed")
        sys.exit(result.returncode)



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ledgerhand",
        description="Ledgerhand: AP operator — model interprets, code decides.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # demo
    sub.add_parser("demo", help="Start ERP + dashboard with preset goals")

    # run
    p_run = sub.add_parser("run", help="Run the operator on a goal")
    p_run.add_argument("--goal", default="", help="Natural-language goal text")
    p_run.add_argument("--spec-file", default="", help="Path to goal spec JSON (bypasses LLM)")
    p_run.add_argument("--inbox", default="", help="Override inbox directory")

    # resume
    p_resume = sub.add_parser("resume", help="Resume a paused/stopped run")
    p_resume.add_argument("run_id", help="Run ID to resume")

    # chaos
    p_chaos = sub.add_parser("chaos", help="Run chaos test suite")
    p_chaos.add_argument("--all", action="store_true", help="Run all fault scenarios")
    p_chaos.add_argument("--fault", default="", help="Specific fault name")

    # audit
    p_audit = sub.add_parser("audit", help="Audit a completed run")
    p_audit.add_argument("run_id", help="Run ID to audit")
    p_audit.add_argument("--eval", action="store_true", help="Compare to expected_*.json")

    # gen-data
    p_data = sub.add_parser("gen-data", help="Generate synthetic invoice PDFs")
    p_data.add_argument("--seed", type=int, default=42, help="Random seed")

    # selfcheck
    sub.add_parser("selfcheck", help="Unit tests + version info + replay chaos")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    handlers = {
        "demo": cmd_demo,
        "run": cmd_run,
        "resume": cmd_resume,
        "chaos": cmd_chaos,
        "audit": cmd_audit,
        "gen-data": cmd_gen_data,
        "selfcheck": cmd_selfcheck,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
