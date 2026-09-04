from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import wraps
from time import monotonic

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai_gateway import AiUsage
from app.ai_transport_errors import AiProviderResultUnknown
from app.models import AiProvider, AiProviderAttempt
from .provider_http_exchanges import settle_provider_exchanges


TOKENS_PER_PRICE_UNIT = 1000


@dataclass(frozen=True)
class ProviderAttemptClock:
    started_at: float

    @classmethod
    def start(cls) -> ProviderAttemptClock:
        return cls(monotonic())

    def latency_ms(self) -> int:
        return max(0, round((monotonic() - self.started_at) * 1000))


def _preserve_unknown_after_call(record):
    @wraps(record)
    def wrapped(*args, **kwargs):
        try:
            return record(*args, **kwargs)
        except AiProviderResultUnknown:
            raise
        except Exception as exc:
            if kwargs.get("http_chain_id"):
                raise AiProviderResultUnknown("provider_attempt_and_exchange_settlement_unproven") from exc
            if kwargs.get("outcome") in {"success", "provider_result_unknown"}:
                raise AiProviderResultUnknown("provider_attempt_persistence_unproven_after_call") from exc
            raise
    return wrapped


@_preserve_unknown_after_call
def record_provider_attempt(
    session: Session,
    config: dict,
    provider: AiProvider,
    *,
    purpose: str,
    priority: int,
    model_name: str,
    request_text: str,
    outcome: str,
    provider_request_id: str = "",
    error_code: str = "",
    latency_ms: int = 0,
    usage: AiUsage | None = None,
    http_chain_id: str = "",
) -> AiProviderAttempt | None:
    job_id = str(config.get("_generation_job_id") or "")
    route_set_id = str(config.get("_ai_provider_route_set_id") or "")
    if not job_id and not route_set_id:
        return None
    if not job_id:
        raise RuntimeError("ai_provider_attempt_binding_incomplete")
    attempt_index = _next_attempt_index(session, job_id, purpose=purpose)
    current_usage = usage or AiUsage()
    cost_amount = _usage_cost(provider, current_usage)
    attempt = AiProviderAttempt(
        generation_job_id=job_id,
        purpose=purpose,
        route_set_id=route_set_id or None,
        route_set_revision=int(config.get("_ai_provider_route_set_revision") or 0),
        provider_id=provider.id,
        model_name=model_name,
        priority=priority,
        attempt_index=attempt_index,
        provider_request_id=provider_request_id[:200],
        request_hash=hashlib.sha256(request_text.encode("utf-8")).hexdigest(),
        outcome=outcome,
        error_code=error_code[:80],
        latency_ms=latency_ms,
        prompt_tokens=max(0, current_usage.prompt_tokens),
        completion_tokens=max(0, current_usage.completion_tokens),
        cached_tokens=max(0, current_usage.cached_tokens),
        cost_amount=cost_amount,
        currency=provider.currency,
    )
    session.add(attempt)
    settle_provider_exchanges(session, config, provider_id=provider.id, request_id=provider_request_id,
                              outcome=outcome, chain_id=http_chain_id)
    session.commit()
    return attempt


def _next_attempt_index(session, job_id, *, purpose):
    return int(session.scalar(select(func.count(AiProviderAttempt.id)).where(
        AiProviderAttempt.generation_job_id == job_id, AiProviderAttempt.purpose == purpose,
    )) or 0) + 1


def _usage_cost(provider: AiProvider, usage: AiUsage) -> float:
    if not provider.is_billable or not usage.billable:
        return 0.0
    input_cost = (
        usage.prompt_tokens * provider.input_price_per_1k / TOKENS_PER_PRICE_UNIT
    )
    output_cost = (
        usage.completion_tokens * provider.output_price_per_1k / TOKENS_PER_PRICE_UNIT
    )
    return round(input_cost + output_cost, 6)


__all__ = ["ProviderAttemptClock", "record_provider_attempt"]
