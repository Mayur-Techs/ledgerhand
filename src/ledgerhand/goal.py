"""Goal compiler — natural language to GoalSpec.

Why: the GoalSpec is the single source of truth for what the run
is allowed to do. It is compiled once, shown to the user as editable
JSON, confirmed, and then immutable for the rest of the run.
"""
from __future__ import annotations
import json
from .models import GoalSpec
from .config import Config
from .llm.prompts import GOAL_COMPILE_PROMPT, PROMPT_VERSION_GOAL
from .llm.client import complete_json


def compile_goal(goal_text: str, config: Config, llm_mode: str = "replay") -> GoalSpec:
    """Compile natural language goal to GoalSpec.
    
    Silent goal → fail-safe defaults (schedule_payments=False).
    Ambiguous goal → clarifications filled, spec stays AWAITING_SPEC.
    Above max_delegable_paise → clamped + note added.
    """
    prompt = GOAL_COMPILE_PROMPT.format(
        goal_text=goal_text,
        max_delegable_paise=config.max_delegable_paise,
    )
    
    raw = complete_json(
        prompt=prompt,
        schema_name="GoalSpec",
        prompt_version=PROMPT_VERSION_GOAL,
        input_text=goal_text,
        model=config.llm_model,
        llm_mode=llm_mode,
    )
    
    spec = GoalSpec(**raw)

    # Clamp auto_pay_max_paise to max_delegable
    auto_max = spec.authority.get("auto_pay_max_paise", 0)
    if auto_max > config.max_delegable_paise:
        spec.authority["auto_pay_max_paise"] = config.max_delegable_paise
        spec.clarifications.append(
            f"auto_pay_max clamped from {auto_max} to {config.max_delegable_paise} (org limit)"
        )

    # Deep-copy the spec so the compilation draft cannot be mutated by callers.
    # The returned spec is the "confirmed" version for this run.
    return spec.confirm()


def spec_from_file(path: str) -> GoalSpec:
    """Load a GoalSpec from a JSON file (bypasses LLM entirely).

    Why: allows editing the compiled spec, running --spec-file in CI,
    and running the full system without any LLM key for batch tests.
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    # confirm() deep-copies — the file spec is immutable once loaded
    return GoalSpec(**data).confirm()


def spec_to_json(spec: GoalSpec) -> str:
    """Serialize GoalSpec to pretty JSON for display and editing."""
    return json.dumps(spec.model_dump(), indent=2, ensure_ascii=False)