from __future__ import annotations

import hashlib
from dataclasses import dataclass
from time import monotonic

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AiProvider, AiProviderAttempt


@dataclass(frozen=True)
class ProviderAttemptClock:
    started_at: float

    @classmethod
    def start(cls) -> ProviderAttemptClock:
        return cls(monotonic())

    def latency_ms(self) -> int:
        return max(0, round((monotonic() - self.started_at) * 1000))


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
    error_code: str = "",
    latency_ms: int = 0,
    total_tokens: int = 0,
) -> None:
    job_id = str(config.get("_generation_job_id") or "")
    route_set_id = str(config.get("_ai_provider_route_set_id") or "")
    if not job_id and not route_set_id:
        return
    if not job_id or not route_set_id:
        raise RuntimeError("ai_provider_attempt_binding_incomplete")
    attempt_index = int(session.scalar(select(func.count(AiProviderAttempt.id)).where(
        AiProviderAttempt.generation_job_id == job_id,
        AiProviderAttempt.purpose == purpose,
    )) or 0) + 1
    session.add(AiProviderAttempt(
        generation_job_id=job_id,
        purpose=purpose,
        route_set_id=route_set_id,
        route_set_revision=int(config.get("_ai_provider_route_set_revision") or 0),
        provider_id=provider.id,
        model_name=model_name,
        priority=priority,
        attempt_index=attempt_index,
        request_hash=hashlib.sha256(request_text.encode("utf-8")).hexdigest(),
        outcome=outcome,
        error_code=error_code[:80],
        latency_ms=latency_ms,
        completion_tokens=max(0, total_tokens),
    ))
    session.commit()


__all__ = ["ProviderAttemptClock", "record_provider_attempt"]
