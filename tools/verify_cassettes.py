"""Verify that cassette keys match the actual PDF text."""
import sys, os
sys.path.insert(0, 'src')
os.environ['LH_TEST'] = '1'
os.environ['LLM_MODE'] = 'replay'

from ledgerhand.llm.cassette import cassette_key, load_cassette
from ledgerhand.llm.prompts import PROMPT_VERSION_EXTRACT
from pathlib import Path

import pdfplumber

batches = [
    ('data/batch_a', ['A01','A02','A03','A04','A05','A06','A07','A08','A09','A10']),
    ('data/batch_b', ['B01','B02','B03','B04','B05']),
]

hits = 0
misses = []

for folder, ids in batches:
    for inv_id in ids:
        pdf = Path(folder) / f"{inv_id}.pdf"
        if not pdf.exists():
            misses.append(f"PDF missing: {pdf}")
            continue
        with pdfplumber.open(pdf) as p:
            text = '\n'.join(pg.extract_text() or '' for pg in p.pages)
        key = cassette_key(PROMPT_VERSION_EXTRACT, text, 'InvoiceDraft')
        c = load_cassette(key)
        if c:
            vendor = c.get('vendor_name_text', '?')
            inv_no = c.get('invoice_no_text', '?')
            inj = c.get('injection_signals', [])
            bank = c.get('bank_account_text', '')
            print(f"  {inv_id}: OK  vendor={vendor!r:35s} inv={inv_no!r} inj={len(inj)} bank={bank!r}")
            hits += 1
        else:
            misses.append(f"{inv_id}: MISS  key={key}")
            print(f"  {inv_id}: MISS  key={key}")

print(f"\n{hits}/15 cassettes matched. Misses: {len(misses)}")
if misses:
    for m in misses:
        print(f"  !! {m}")
    sys.exit(1)
