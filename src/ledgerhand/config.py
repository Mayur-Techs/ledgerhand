"""Configuration loaded from .env and policy.yaml.

Why: centralise all env reads so the rest of the codebase never calls
os.getenv directly; makes testing easy (swap Config).
"""
from __future__ import annotations
import os
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any


_REPO_ROOT = Path(__file__).parent.parent.parent  # ledgerhand/


def _load_policy() -> dict[str, Any]:
    path = _REPO_ROOT / "policy.yaml"
    if not path.exists():
        return {}
    with path.open() as f:
        return yaml.safe_load(f) or {}


@dataclass(frozen=True)
class RetryConfig:
    max_attempts: int = 2
    prepare_max_failures: int = 2


@dataclass(frozen=True)
class VerifyConfig:
    poll_timeout_s: float = 8.0
    poll_interval_s: list[float] = field(default_factory=lambda: [0.5, 1.0, 2.0])


@dataclass(frozen=True)
class Config:
    # org
    max_delegable_paise: int = 10_000_000
    hard_ceiling_paise: int = 50_000_000
    price_tolerance_pct: float = 3.0
    qty_tolerance_pct: float = 0.0
    rounding_tolerance_paise: int = 100
    date_format: str = "DMY"
    near_dup_max_edit_distance: int = 2
    require_po: bool = True
    require_receipt: bool = True
    always_human: tuple[str, ...] = ("BANK_CHANGED", "DUPLICATE_NEAR", "BUSINESS_KEY_CONFLICT", "VENDOR_UNKNOWN")
    unsupported: tuple[str, ...] = ("CREDIT_NOTE", "MULTI_PO", "FOREIGN_CURRENCY")
    approval_ttl_hours: int = 24
    skip_weekend_payment_dates: bool = True
    # extraction
    min_confidence_critical: float = 0.8
    # runtime
    max_steps_per_task: int = 35
    max_run_minutes: int = 15
    wait_for_approvals_s: int = 120
    retry: RetryConfig = field(default_factory=RetryConfig)
    verify: VerifyConfig = field(default_factory=VerifyConfig)
    # browser
    allowed_paths: tuple[str, ...] = ("/login", "/vendors", "/pos", "/bills", "/payments")
    # env-sourced
    llm_model: str = "gpt-4o-mini"
    llm_mode: str = "replay"  # live | record | replay
    lb_base_url: str = "http://localhost:8001"
    lb_username: str = "admin"
    lb_password: str = "ledger123"
    lb_enforce_unique: bool = True
    lb_ui_variant: str = "v1"
    lh_test: bool = False
    lh_crash_at: str = ""  # e.g. "after_dispatch:1"
    injection_high: tuple[str, ...] = field(default_factory=tuple)
    injection_medium: tuple[str, ...] = field(default_factory=tuple)


def load_config() -> Config:
    """Load policy.yaml + env vars and return a frozen Config."""
    from dotenv import load_dotenv
    load_dotenv()
    p = _load_policy()
    org = p.get("org", {})
    rt = p.get("runtime", {})
    ext = p.get("extraction", {})
    br = p.get("browser", {})
    inj = p.get("injection", {})
    retry_d = rt.get("retry", {})
    verify_d = rt.get("verify", {})

    return Config(
        max_delegable_paise=org.get("max_delegable_paise", 10_000_000),
        hard_ceiling_paise=org.get("hard_ceiling_paise", 50_000_000),
        price_tolerance_pct=float(org.get("price_tolerance_pct", 3.0)),
        qty_tolerance_pct=float(org.get("qty_tolerance_pct", 0.0)),
        rounding_tolerance_paise=int(org.get("rounding_tolerance_paise", 100)),
        date_format=org.get("date_format", "DMY"),
        near_dup_max_edit_distance=org.get("near_duplicate", {}).get("max_edit_distance", 2),
        require_po=org.get("require_po", True),
        require_receipt=org.get("require_receipt", True),
        always_human=tuple(org.get("always_human", ["BANK_CHANGED", "DUPLICATE_NEAR", "BUSINESS_KEY_CONFLICT", "VENDOR_UNKNOWN"])),
        unsupported=tuple(org.get("unsupported", ["CREDIT_NOTE", "MULTI_PO", "FOREIGN_CURRENCY"])),
        approval_ttl_hours=int(org.get("approval_ttl_hours", 24)),
        skip_weekend_payment_dates=org.get("skip_weekend_payment_dates", True),
        min_confidence_critical=float(ext.get("min_confidence_critical", 0.8)),
        max_steps_per_task=int(rt.get("max_steps_per_task", 35)),
        max_run_minutes=int(rt.get("max_run_minutes", 15)),
        wait_for_approvals_s=int(rt.get("wait_for_approvals_s", 120)),
        retry=RetryConfig(
            max_attempts=int(retry_d.get("max_attempts", 2)),
            prepare_max_failures=int(retry_d.get("prepare_max_failures", 2)),
        ),
        verify=VerifyConfig(
            poll_timeout_s=float(verify_d.get("poll_timeout_s", 8.0)),
            poll_interval_s=verify_d.get("poll_interval_s", [0.5, 1.0, 2.0]),
        ),
        allowed_paths=tuple(br.get("allowed_paths", ["/login", "/vendors", "/pos", "/bills", "/payments"])),
        llm_model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
        llm_mode=os.getenv("LLM_MODE", "replay"),
        lb_base_url=os.getenv("LB_BASE_URL", "http://localhost:8001"),
        lb_username=os.getenv("LB_USERNAME", "admin"),
        lb_password=os.getenv("LB_PASSWORD", "ledger123"),
        lb_enforce_unique=os.getenv("LB_ENFORCE_UNIQUE", "1") != "0",
        lb_ui_variant=os.getenv("LB_UI_VARIANT", "v1"),
        lh_test=os.getenv("LH_TEST", "") == "1",
        lh_crash_at=os.getenv("LH_CRASH_AT", ""),
        injection_high=tuple(inj.get("high", [])),
        injection_medium=tuple(inj.get("medium", [])),
    )


# Module-level singleton — import this everywhere
CONFIG: Config = load_config()
