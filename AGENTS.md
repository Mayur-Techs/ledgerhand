# Ledgerhand — AGENTS.md (always-on rules for AI coding tools)

This file is the short rule set. The full build contract is `docs/PLAN.md`.

## Non-negotiable rules
1. Money is always integer paise. Never float. Never string arithmetic.
2. The operator never writes control.db; the dashboard never writes ledger.db.
3. Only mutation.py may call prepare_*/commit_* skills.
4. Every mutation is lookup-first: check the world state BEFORE acting.
5. No success claim without re-reading the application (G3).
6. Exceptions after mark_dispatched are all UNKNOWN — catch Exception, not BaseException.
7. No async in the operator process.
8. Files ≤ 250 lines. Functions ≤ 40 lines.
9. Comments explain WHY, not what.
10. LH_TEST=1 is required for any test-only shortcut.

## Layout
See docs/PLAN.md §3.4 for the full directory structure.
