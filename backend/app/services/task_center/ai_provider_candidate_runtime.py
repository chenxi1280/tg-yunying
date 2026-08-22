from __future__ import annotations

import socket
import urllib.error
from collections.abc import Iterator
from dataclasses import dataclass, replace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai_gateway import (
    AiEmptyFinalContentError,
    AiGenerationResult,
    AiProviderCredentials,
    AiProviderRateLimited,
    AiRequestDeadlineExceeded,
    normalize_ai_model_name,
)
from app.models import AiProvider, AiProviderHealthStatus
from app.services._common import _now, ai_gateway
from app.services.ai_config import ai_provider_credentials
from app.services.task_center.ai_generation_contract import (
    AI_GENERATION_UNAVAILABLE_MESSAGE,
    LONG_RUNNING_AI_PURPOSES,
    AiGenerationUnavailable,
    ProviderRouteDeferred,
)
from app.services.task_center.provider_admission import (
    ProviderAdmissionBlocked,
    ProviderProbeLease,
    begin_provider_call,
    extend_provider_cooldown,
    provider_admission_key,
    release_provider_probe,
    settle_provider_success,
)


PROVIDER_ROUTE_RETRY_SECONDS = 30
AI_PROVIDER_QUOTA_EXHAUSTED_MARKERS = (
    "quota exhausted",
    "insufficient quota",
    "insufficient balance",
    "quota_exhausted",
    "余额不足",
    "配额不足",
    "配额耗尽",
)


@dataclass(frozen=True)
class ProviderDraftRequest:
    prompt: str
    count: int
    topic: str
    tone: str
    persona_set: tuple[str, ...]
    temperature: float
    max_tokens: int
    system_prompt: str | None
    timeout: int


@dataclass(frozen=True)
class ProviderCandidatePolicy:
    model_name: str
    required_model_family: str
    allow_quota_rotation: bool
    purpose: str
    close_transaction_before_external: bool
    route_provider_ids: tuple[int, ...] = ()
    route_models: dict[int, str] | None = None


@dataclass(frozen=True)
class DraftAttemptOutcome:
    result: AiGenerationResult | None
    error: Exception | None
    route_retryable: bool
    continue_candidates: bool


def generate_with_provider_candidates(
    session: Session,
    provider: AiProvider,
    request: ProviderDraftRequest,
    *,
    policy: ProviderCandidatePolicy,
) -> AiGenerationResult:
    providers, provider_calls = draft_provider_calls(session, provider, policy)
    failures = _CandidateFailures()
    attempts: list[dict] = []
    for candidate, credentials in provider_calls:
        outcome = attempt_provider_draft(
            session,
            candidate,
            credentials,
            request=request,
            policy=policy,
            has_more=candidate != providers[-1],
        )
        attempts.append(_candidate_attempt(candidate, credentials, outcome.error))
        if outcome.result is not None:
            return replace(
                outcome.result,
                provider_id=candidate.id,
                provider_name=candidate.provider_name,
                model_name=str(getattr(credentials, "model_name", candidate.model_name) or ""),
                provider_attempts=tuple(attempts),
            )
        failures = failures.add(outcome)
        if not outcome.continue_candidates:
            break
    failures.raise_final(policy, len(providers))
    raise RuntimeError("provider candidate resolution returned without a result")


def _candidate_attempt(
    provider: AiProvider,
    credentials: AiProviderCredentials,
    error: Exception | None,
) -> dict:
    return {
        "provider_id": provider.id,
        "model": str(getattr(credentials, "model_name", provider.model_name) or ""),
        "outcome": "success" if error is None else "failed",
        "error_code": _candidate_error_code(error),
    }


def _candidate_error_code(error: Exception | None) -> str:
    if isinstance(error, ProviderAdmissionBlocked):
        return error.reason
    return type(error).__name__ if error is not None else ""


@dataclass(frozen=True)
class _CandidateFailures:
    last_error: Exception | None = None
    blocked_error: ProviderAdmissionBlocked | None = None
    route_retryable: int = 0
    retry_seconds: int = PROVIDER_ROUTE_RETRY_SECONDS

    def add(self, outcome: DraftAttemptOutcome) -> _CandidateFailures:
        blocked = outcome.error if isinstance(outcome.error, ProviderAdmissionBlocked) else None
        return _CandidateFailures(
            last_error=outcome.error,
            blocked_error=blocked or self.blocked_error,
            route_retryable=self.route_retryable + int(outcome.route_retryable),
            retry_seconds=max(self.retry_seconds, blocked.wait_seconds if blocked else 0),
        )

    def raise_final(self, policy: ProviderCandidatePolicy, provider_count: int) -> None:
        if policy.route_provider_ids and self.route_retryable == provider_count:
            detail = self.last_error or self.blocked_error or "all_route_candidates_temporarily_unavailable"
            raise ProviderRouteDeferred(
                str(detail), retry_after_seconds=self.retry_seconds,
            ) from self.last_error
        if self.blocked_error is not None and (
            self.last_error is None or self.last_error is self.blocked_error
        ):
            raise self.blocked_error
        raise_provider_generation_failure(self.last_error, policy.purpose)


def draft_provider_calls(
    session: Session,
    provider: AiProvider,
    policy: ProviderCandidatePolicy,
) -> tuple[list[AiProvider], Iterator[tuple[AiProvider, AiProviderCredentials]]]:
    providers = provider_candidates(
        session,
        provider,
        required_model_family=policy.required_model_family,
        allow_quota_rotation=policy.allow_quota_rotation,
        route_provider_ids=policy.route_provider_ids,
    )
    calls = provider_calls(
        session,
        providers,
        policy.model_name,
        close_transaction_before_external=policy.close_transaction_before_external,
        route_models=policy.route_models,
    )
    return providers, calls


def attempt_provider_draft(
    session: Session,
    candidate: AiProvider,
    credentials: AiProviderCredentials,
    *,
    request: ProviderDraftRequest,
    policy: ProviderCandidatePolicy,
    has_more: bool,
) -> DraftAttemptOutcome:
    route_bound = bool(policy.route_provider_ids)
    try:
        lease = begin_provider_call(candidate)
    except ProviderAdmissionBlocked as exc:
        return DraftAttemptOutcome(None, exc, route_bound, True)
    try:
        result = generate_provider_drafts(candidate, credentials, request, lease=lease)
        return DraftAttemptOutcome(result, None, False, False)
    except ProviderAdmissionBlocked as exc:
        return DraftAttemptOutcome(None, exc, route_bound, route_bound and has_more)
    except Exception as exc:
        return provider_draft_failure(
            session, candidate, error=exc, policy=policy, has_more=has_more,
        )


def provider_draft_failure(
    session: Session,
    candidate: AiProvider,
    *,
    error: Exception,
    policy: ProviderCandidatePolicy,
    has_more: bool,
) -> DraftAttemptOutcome:
    if is_ai_provider_quota_exhausted(error):
        mark_provider_quota_exhausted(candidate, error)
        if policy.close_transaction_before_external:
            session.add(candidate)
            session.commit()
        return DraftAttemptOutcome(
            None,
            error,
            bool(policy.route_provider_ids),
            has_more,
        )
    if policy.route_provider_ids and route_transport_failure(error):
        return DraftAttemptOutcome(None, error, True, True)
    return DraftAttemptOutcome(None, error, False, False)


def generate_provider_drafts(
    provider: AiProvider,
    credentials: AiProviderCredentials,
    request: ProviderDraftRequest,
    *,
    lease: ProviderProbeLease | None,
) -> AiGenerationResult:
    try:
        result = ai_gateway.generate_drafts(
            credentials,
            request.prompt,
            count=request.count,
            topic=request.topic,
            tone=request.tone,
            persona_set=list(request.persona_set),
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            system_prompt=request.system_prompt,
            timeout=request.timeout,
        )
    except AiProviderRateLimited as exc:
        defer_rate_limited_provider(provider, lease, exc)
    except Exception:
        release_provider_probe(lease)
        raise
    settle_provider_success(lease)
    return result


def defer_rate_limited_provider(
    provider: AiProvider,
    lease: ProviderProbeLease | None,
    error: AiProviderRateLimited,
) -> None:
    extend_provider_cooldown(
        provider,
        error.retry_after_seconds,
        reason=f"http_429:{error.detail[:120]}",
    )
    release_provider_probe(lease)
    raise ProviderAdmissionBlocked(
        provider_admission_key(provider),
        error.retry_after_seconds or 0,
        reason="provider_rate_limited",
    ) from error


def provider_candidates(
    session: Session,
    provider: AiProvider,
    *,
    required_model_family: str,
    allow_quota_rotation: bool,
    route_provider_ids: tuple[int, ...] = (),
) -> list[AiProvider]:
    if route_provider_ids:
        return ordered_route_providers(session, route_provider_ids)
    providers = [provider]
    if allow_quota_rotation:
        providers.extend(quota_rotation_providers(session, provider, required_model_family))
    return providers


def provider_calls(
    session: Session,
    providers: list[AiProvider],
    model_name: str,
    *,
    close_transaction_before_external: bool,
    route_models: dict[int, str] | None = None,
) -> Iterator[tuple[AiProvider, AiProviderCredentials]]:
    models = route_models or {}
    if close_transaction_before_external:
        for candidate in providers:
            session.expunge(candidate)
        session.commit()
    return iter_provider_calls(providers, model_name, models)


def iter_provider_calls(
    providers: list[AiProvider],
    model_name: str,
    models: dict[int, str],
) -> Iterator[tuple[AiProvider, AiProviderCredentials]]:
    for candidate in providers:
        yield candidate, ai_credentials(
            candidate,
            models.get(candidate.id, model_name),
            route_bound=bool(models),
        )


def ai_credentials(
    provider: AiProvider,
    model_name: str,
    *,
    route_bound: bool = False,
) -> AiProviderCredentials:
    credentials = ai_provider_credentials(provider, route_bound=route_bound)
    if model_name.strip():
        return replace(credentials, model_name=normalize_ai_model_name(model_name))
    return credentials


def ordered_route_providers(session: Session, provider_ids: tuple[int, ...]) -> list[AiProvider]:
    providers = list(session.scalars(select(AiProvider).where(
        AiProvider.id.in_(provider_ids),
        AiProvider.credential_enabled.is_(True),
        AiProvider.health_status == AiProviderHealthStatus.HEALTHY.value,
    )))
    by_id = {item.id: item for item in providers}
    ordered = [by_id[provider_id] for provider_id in provider_ids if provider_id in by_id]
    if not ordered:
        detail = "provider_route_candidates_empty"
        raise AiGenerationUnavailable(f"{AI_GENERATION_UNAVAILABLE_MESSAGE}：{detail}")
    return ordered


def route_transport_failure(error: Exception) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return int(error.code or 0) >= 500
    if isinstance(error, (TimeoutError, ConnectionError, socket.timeout, urllib.error.URLError)):
        return True
    if isinstance(error, AiRequestDeadlineExceeded):
        return True
    return not isinstance(error, AiEmptyFinalContentError) and is_ai_provider_quota_exhausted(error)


def quota_rotation_providers(
    session: Session,
    provider: AiProvider,
    required_family: str,
) -> list[AiProvider]:
    if required_family != "mimo":
        return []
    providers = session.scalars(
        select(AiProvider)
        .where(
            AiProvider.is_active.is_(True),
            AiProvider.health_status == AiProviderHealthStatus.HEALTHY.value,
        )
        .order_by(AiProvider.id.asc())
    ).all()
    return [
        candidate for candidate in providers
        if candidate.id != provider.id and provider_matches_family(candidate, required_family)
    ]


def provider_matches_family(provider: AiProvider, required_family: str) -> bool:
    if not required_family:
        return True
    model_name = normalize_ai_model_name(provider.model_name)
    return required_family == "mimo" and "mimo" in model_name.lower()


def is_ai_provider_quota_exhausted(error: Exception) -> bool:
    detail = str(error).lower()
    return any(marker in detail for marker in AI_PROVIDER_QUOTA_EXHAUSTED_MARKERS)


def mark_provider_quota_exhausted(provider: AiProvider, error: Exception) -> None:
    provider.health_status = AiProviderHealthStatus.UNHEALTHY.value
    provider.last_check_at = _now()
    provider.last_error = f"AI provider quota exhausted: {str(error)[:300]}"
    provider.updated_at = _now()


def raise_provider_generation_failure(error: Exception | None, purpose: str) -> None:
    if purpose in LONG_RUNNING_AI_PURPOSES:
        raise AiGenerationUnavailable(f"{AI_GENERATION_UNAVAILABLE_MESSAGE}：{error}") from error
    if error:
        raise error
    raise RuntimeError("AI provider generation failed without detail")
