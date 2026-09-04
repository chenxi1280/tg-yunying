"""Apply frozen generation budgets immediately before each external invocation."""
from datetime import datetime
from math import floor
import time

from app.services._common import _now
from app.timezone import as_beijing

from .ai_generation_contract import AiGenerationUnavailable


TIMING_CONFIG_KEY = "_ai_execution_timing"
MAX_LLM_INVOCATION_SECONDS = 15


def provider_invocation_options(config: dict | None, *, legacy_timeout: int) -> dict:
    started = time.monotonic()
    timeout = provider_invocation_timeout(config, legacy_timeout=legacy_timeout)
    if TIMING_CONFIG_KEY not in (config or {}):
        return {"timeout": timeout}
    return {"timeout": timeout, "request_deadline": started + timeout}


def provider_invocation_timeout(config: dict | None, *, legacy_timeout: int, now_value=None) -> int:
    config = config or {}
    snapshot = config.get(TIMING_CONFIG_KEY)
    if snapshot is None and config.get("engagement_contract_version") != "unified_engagement_v1":
        return legacy_timeout
    if not isinstance(snapshot, dict) or snapshot.get("version") != "generation_timing_v1":
        raise AiGenerationUnavailable("generation_timing_snapshot_missing")
    if snapshot.get("provider_calls_allowed") is False:
        raise AiGenerationUnavailable("generation_timing_recovery_provider_call_forbidden")
    ceiling = snapshot.get("llm_timeout_ceiling_seconds")
    if type(ceiling) is not int or not 0 < ceiling <= MAX_LLM_INVOCATION_SECONDS:
        raise AiGenerationUnavailable("generation_timing_llm_ceiling_invalid")
    try:
        deadline = datetime.fromisoformat(snapshot["candidate_ready_deadline_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AiGenerationUnavailable("generation_timing_deadline_invalid") from exc
    remaining = floor((as_beijing(deadline) - as_beijing(now_value or _now())).total_seconds())
    if remaining < 1:
        raise AiGenerationUnavailable("generation_timing_invocation_budget_exhausted")
    return min(ceiling, remaining)
