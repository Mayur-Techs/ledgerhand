"""LLM client adapter — live | record | replay.

Why: replay mode lets reviewers and CI run without an API key.
The cassette is keyed by (prompt_version + input_hash + schema_name)
so prompt changes invalidate old cassettes.
"""
from __future__ import annotations
import json
import os
from .cassette import cassette_key, load_cassette, save_cassette


class CassetteNotFoundError(Exception):
    pass


def complete_json(
    prompt: str,
    schema_name: str,
    prompt_version: str,
    input_text: str,
    model: str = "",
    llm_mode: str = "replay",
) -> dict:
    """Make a structured JSON completion. Returns parsed dict.
    
    In replay mode: load from cassette or raise CassetteNotFoundError.
    In record mode: call the LLM, save to cassette, return result.
    In live mode: call the LLM, do not save.
    """
    key = cassette_key(prompt_version, input_text, schema_name)
    
    if llm_mode == "replay":
        cassette = load_cassette(key)
        if cassette is None:
            raise CassetteNotFoundError(
                f"No cassette for key={key} schema={schema_name}. "
                f"Run with LLM_MODE=record and an API key to record it, "
                f"or edit the spec manually."
            )
        return cassette
    
    # live or record: call the LLM
    response = _call_llm(prompt, model)
    
    if llm_mode == "record":
        save_cassette(key, response)
    
    return response


def _call_llm(prompt: str, model: str) -> dict:
    """Call litellm and parse JSON response."""
    try:
        import litellm
    except ImportError:
        raise ImportError("litellm not installed. Run: pip install litellm")
    
    response = litellm.completion(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        response_format={"type": "json_object"},
    )
    text = response.choices[0].message.content
    return json.loads(text)