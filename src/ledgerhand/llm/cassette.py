"""Cassette recorder/replayer for deterministic LLM calls.

Why: replay mode lets reviewers run the full system without any API key.
Cassettes are keyed by (prompt_version + input_hash + schema_name) so
a change in the prompt invalidates old cassettes (forcing re-record).
"""
from pathlib import Path
import hashlib
import json

CASSETTE_DIR = Path("cassettes")

def cassette_key(prompt_version: str, input_text: str, schema_name: str) -> str:
    """sha256(prompt_version + input_hash + schema_name)."""
    input_hash = hashlib.sha256(input_text.encode()).hexdigest()
    raw = f"{prompt_version}:{input_hash}:{schema_name}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

def load_cassette(key: str) -> dict | None:
    """Load a recorded response. Return None if not found."""
    path = CASSETTE_DIR / f"{key}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))

def save_cassette(key: str, response: dict) -> None:
    """Save a response for future replay."""
    CASSETTE_DIR.mkdir(exist_ok=True)
    path = CASSETTE_DIR / f"{key}.json"
    path.write_text(json.dumps(response, indent=2, ensure_ascii=False), encoding="utf-8")