# Ledgerhand

**AP operator — model interprets, deterministic code decides, acts, and verifies.**

> Deadline: 4 Oct 2026 4 PM

---

## What it is

Ledgerhand automates Accounts Payable (AP) invoice processing against a legacy ERP ("LedgerBooks"). It extracts invoice data from PDFs using an LLM, runs every check deterministically in code, and only acts if all checks pass. Every mutation is looked up before acting (G3: evidence-first), and the hash-chained ledger prevents duplicate submissions (G1) and authority overreach (G2).

**Three guarantees:**

| | Guarantee | How |
|---|-----------|-----|
| G1 | **No duplicates** | Intent ledger keyed on `(kind, business_key)`; UNIQUE marker in ERP `Internal Ref` |
| G2 | **Authority-bound** | Per-intent `authorize_again` check; hard ceiling blocks anything above ₹5,00,000 |
| G3 | **Evidence-first** | Re-read the ERP after every mutation; never claim success without a re-read |

## Honest AI Disclosure & Status

**This codebase was constructed largely by an AI (Antigravity).** It is a prototype meant to demonstrate safety architecture, not a production-ready application.
- **Do not** use this in a real financial environment.
- **Do not** assume all edge cases are handled.
- "All tests pass" means the 99 unit tests and 5 chaos integration tests pass against the mock ERP. It does not mean the system is flawless.
- The project implements safety invariants (I1-I8) as an architectural demonstration of how to bind a non-deterministic LLM with deterministic code.

---

## Quick start

```powershell
# 1. Install (editable)
C:\Python314\python.exe -m pip install -e ".[dev]"
playwright install chromium

# 2. Copy env
copy .env.example .env

# 3. Generate synthetic data
C:\Python314\python.exe -m ledgerhand gen-data --seed 42

# 4. Record cassettes (pre-seeded, no API key needed)
C:\Python314\python.exe tools\record_cassettes.py

# 5. Run unit tests
C:\Python314\python.exe -m pytest tests/ -m "not integration" -v

# 6. Demo (starts ERP + dashboard, opens browser)
C:\Python314\python.exe -m ledgerhand demo
```

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  LedgerBooks (mock ERP)      :8001                  │
│  Dashboard                   :8000                  │
└──────────────┬──────────────────────────────────────┘
               │  Playwright (browser)
┌──────────────▼──────────────────────────────────────┐
│  Operator process                                   │
│  ┌──────────┐ ┌────────────┐ ┌──────────────────┐  │
│  │ extract  │ │  checks/   │ │    mutation       │  │
│  │ (LLM +  │ │  policy    │ │  lookup-first     │  │
│  │ pdfplumb)│ │  (code)    │ │  dispatch → verify│  │
│  └──────────┘ └────────────┘ └──────────────────┘  │
│                                                     │
│  ledger.db (WAL, hash chain, single writer)         │
└─────────────────────────────────────────────────────┘
               │  read-only
┌──────────────▼──────────────────────────────────────┐
│  Dashboard reads ledger.db (mode=ro)                │
│  Dashboard writes control.db (commands only)        │
└─────────────────────────────────────────────────────┘
```

**Single-writer rule:** `ledger.db` ← operator only; `control.db` ← dashboard only; `erp.db` ← ERP only.

---

## Stack

| Layer | Choice | Why |
|-------|--------|-----|
| Browser | Playwright (sync) | Stable cross-browser; sync keeps the operator sequential |
| LLM | litellm (any provider) | Single abstraction; replay cassettes need no API key |
| DB | SQLite WAL | No Docker; single-writer per file; hash chain fits natively |
| Framework | FastAPI | Both ERP and dashboard; Jinja2 templates |
| Validation | Pydantic v2 | Validate at every boundary, frozen models |
| Money | Integer paise | No floating-point; every bug is loud |

---

## CLI commands

```
ledgerhand demo          # Start ERP + dashboard, open browser
ledgerhand run           # Run operator on a goal (--goal or --spec-file)
ledgerhand resume        # Resume a paused/stopped run
ledgerhand audit         # Audit a completed run (--eval compares to answer key)
ledgerhand chaos         # Run fault-injection integration tests
ledgerhand gen-data      # Regenerate synthetic invoice PDFs
ledgerhand selfcheck     # Python/SQLite/Playwright version check
```

---

## Running the demo

```powershell
C:\Python314\python.exe -m ledgerhand demo
```

This starts:
- **LedgerBooks ERP** at http://localhost:8001 — login: `admin` / `ledger123`
- **Dashboard** at http://localhost:8000 — opens automatically

Use the **Preset** buttons to load batch goals:
- **Baseline goal (Batch A)** — processes 10 invoices; 3 ENTER, 1 QUARANTINE, 6 HOLD
- **Variation goal (Batch B)** — scope-filtered; 2 ENTER, 1 HOLD, 2 SKIP

---

## Decision table (abbreviated)

| Row | Condition | Bill decision | Pay decision |
|-----|-----------|---------------|--------------|
| 1 | High-severity injection OR low confidence on critical field | QUARANTINE | NONE |
| 2 | Outside goal scope | SKIP | NONE |
| 3 | Our marker found in ERP | SKIP (already done) | → payment step |
| 4 | Unsupported (credit note / multi-PO / foreign currency) | HOLD | NONE |
| 5 | Vendor unknown | HOLD | NONE |
| 6 | Exact duplicate | HOLD | NONE |
| 7 | Conflict (same key, different amount) | HOLD | NONE |
| 8 | Near-duplicate (edit distance ≤ 2) | HOLD | NONE |
| 9 | Bank account changed | HOLD | NONE |
| 10 | PO mismatch / price over tolerance / qty over received | HOLD | NONE |
| 11 | All checks pass, ≤ auto limit | ENTER | AUTO |
| 11 | All checks pass, > auto limit, ≤ ceiling | ENTER | APPROVE |
| 11 | All checks pass, > ceiling | ENTER | BLOCK |

---

## Mutation protocol (G3)

```
lookup-first (assert world state)
  ↓ not found
authorize_again
  ↓
prepare (fill form — no side effect)
mark_dispatched
  ↓  ← crash here → UNKNOWN → retry
commit (click submit)
  ↓  ← exception → UNKNOWN (not FAILED)
verify (re-read with backoff)
  ↓ matches
confirm_intent
```

---

## Cassettes (replay mode)

All 17 cassettes are pre-recorded in `cassettes/`. In `LLM_MODE=replay` no API key is needed.

To re-record (e.g. after changing prompts):
```powershell
$env:LLM_MODE="record"
$env:OPENAI_API_KEY="sk-..."
C:\Python314\python.exe tools\record_cassettes.py
```

---

## Testing

```powershell
# Unit tests (no ERP needed)
C:\Python314\python.exe -m pytest tests/ -m "not integration" -v

# Integration/chaos tests (start ERP first)
C:\Python314\python.exe -m ledgerhand chaos --all

# Audit a run
C:\Python314\python.exe -m ledgerhand audit <run_id> --eval
```

---

## Directory layout

```
ledgerhand/
├─ src/ledgerhand/
│  ├─ cli.py              # Entry point, 7 commands
│  ├─ config.py           # Frozen Config from .env + policy.yaml
│  ├─ models.py           # Pydantic v2 domain models
│  ├─ money.py            # Integer paise utilities
│  ├─ parse.py            # normalize_invoice_no, parse_date, markers, hashes
│  ├─ ledger.py           # SQLite WAL ledger, hash chain, intent store
│  ├─ checks.py           # All deterministic checks (math, vendor, PO, dup, …)
│  ├─ policy.py           # First-match decision table
│  ├─ extract.py          # PDF → InvoiceDraft (pdfplumber + LLM)
│  ├─ goal.py             # NL goal → GoalSpec
│  ├─ mutation.py         # execute_mutation (lookup-first protocol)
│  ├─ verify.py           # Poll ERP after mutation
│  ├─ runner.py           # Sequential orchestrator state machine
│  ├─ audit.py            # Post-run verifier vs answer key
│  ├─ report.py           # Proof Pack ZIP + CSV
│  ├─ browser/
│  │  ├─ driver.py        # Playwright lifecycle + tracing
│  │  ├─ skills.py        # All read/write ERP interactions
│  │  ├─ selectors.py     # Locator ladder (v1/v2 UI drift)
│  │  └─ guards.py        # PathGuard (I8), DialogHandler
│  ├─ llm/
│  │  ├─ client.py        # live | record | replay
│  │  ├─ cassette.py      # Cassette store (SHA-keyed)
│  │  └─ prompts.py       # Goal + extraction prompt templates
│  └─ control/
│     ├─ app.py           # Dashboard FastAPI :8000
│     └─ presets.py       # Demo preset goals
├─ mock_apps/ledgerbooks/ # Fake legacy ERP (FastAPI :8001)
├─ data/                  # Synthetic invoices + answer keys
├─ cassettes/             # Pre-recorded LLM responses
├─ tests/
│  ├─ test_scaffold.py    # Phase 0 gate (4 tests)
│  ├─ test_core.py        # Phase 3 gate (90 tests)
│  ├─ test_audit.py       # Phase 7 gate (5 tests)
│  ├─ test_chaos.py       # Phase 8 integration (3 scenarios)
│  └─ test_crash_recovery.py  # Phase 8 crash recovery (2 scenarios)
├─ tools/
│  ├─ make_data.py        # Deterministic PDF generator
│  └─ record_cassettes.py # Cassette recorder
├─ policy.yaml            # Org policy (tolerances, ceilings, always-human list)
├─ AGENTS.md              # Non-negotiable rules for AI coding tools
└─ pyproject.toml
```
