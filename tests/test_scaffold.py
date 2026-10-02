"""Phase 0 acceptance tests."""
import subprocess
import sys


def test_help():
    result = subprocess.run(
        [sys.executable, "-m", "ledgerhand", "--help"],
        capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "ledgerhand" in result.stdout


def test_selfcheck():
    import os
    env = os.environ.copy()
    env["LH_NO_RECURSIVE_TEST"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "ledgerhand", "selfcheck"],
        capture_output=True, text=True,
        env=env
    )
    assert result.returncode == 0
    # Should print Python version and SQLite version
    assert "Python" in result.stdout
    assert "SQLite" in result.stdout


def test_gitignore_covers_private():
    import subprocess
    from pathlib import Path
    # Find the repo root from this test file's location
    repo_root = Path(__file__).parent.parent
    result = subprocess.run(
        ["git", "check-ignore", "-v", "docs/private/x"],
        capture_output=True, text=True,
        cwd=str(repo_root),
    )
    # Should print a rule (exit code 0 means it IS ignored)
    assert result.returncode == 0, "docs/private/ should be gitignored"


def test_money_parse():
    from ledgerhand.money import parse_paise, paise_to_rupees_str
    assert parse_paise("₹18,450.00") == 1_845_000
    assert parse_paise("Rs. 4,500") == 450_000
    assert parse_paise("₹1,23,456.00") == 12_345_600
    assert paise_to_rupees_str(1_845_000) == "₹18,450.00"
