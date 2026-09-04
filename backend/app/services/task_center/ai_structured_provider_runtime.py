from __future__ import annotations

from .generation_invocation_budget import provider_invocation_options
from .provider_http_tracking import scoped_provider_gateway

import hashlib
from dataclasses import dataclass, replace

from sqlalchemy.orm import Session

from app.ai_gateway import AiProviderCredentials, AiUsage
from app.models import AiProvider
from app.ai_transport_errors import AiProviderResultUnknown
from app.services._common import ai_gateway
from app.services.automation_identity import with_automation_identity
from app.services.task_center.ai_generation_contract import ProviderRouteDeferred
from app.services.task_center.antigravity_schemas import antigravity_schema_for_purpose
from app.services.task_center.ai_provider_attempts import (
    ProviderAttemptClock,
    record_provider_attempt,
)
from app.services.task_center.ai_provider_candidate_runtime import (
    PROVIDER_ROUTE_RETRY_SECONDS,
    is_ai_provider_quota_exhausted,
    mark_provider_quota_exhausted,
    provider_calls,
    provider_candidates,
    raise_provider_generation_failure,
    route_transport_failure,
)
from app.services.task_center.provider_admission import (
    ProviderAdmissionBlocked,
    ProviderProbeLease,
    begin_provider_call,
    release_provider_probe,
    settle_provider_success,
)


AI_CONTENT_REQUEST_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class StructuredProviderRequest:
    system_prompt: str
    user_prompt: str
    config: dict
    temperature: float
    max_tokens: int
    count: int
    purpose: str
    model_name: str
    stage: str
    required_model_family: str

    def request_id(self) -> str:
        job_id = str(self.config.get("_generation_job_id") or "")
        if not job_id:
            return ""
        invocation_key = str(self.config.get("_ai_provider_invocation_key") or "")
        if not invocation_key:
            raise RuntimeError("ai_provider_invocation_key_missing")
        invocation_hash = hashlib.sha256(invocation_key.encode("utf-8")).hexdigest()[:24]
        revision = int(self.config.get("_ai_provider_route_set_revision") or 0)
        return f"agy:{job_id}:{self.purpose}:{self.stage or 'primary'}:r{revision}:{invocation_hash}"


@dataclass(frozen=True)
class StructuredAttemptOutcome:
    result: tuple[object, int] | None
    error: Exception | None
    route_retryable: bool
    continue_candidates: bool


@dataclass(frozen=True)
class _StructuredFailures:
    last_error: Exception | None = None
    blocked_error: ProviderAdmissionBlocked | None = None
    route_retryable: int = 0
    retry_seconds: int = PROVIDER_ROUTE_RETRY_SECONDS

    def add(self, outcome: StructuredAttemptOutcome) -> _StructuredFailures:
        blocked = outcome.error if isinstance(outcome.error, ProviderAdmissionBlocked) else None
        return _StructuredFailures(
            last_error=outcome.error,
            blocked_error=blocked or self.blocked_error,
            route_retryable=self.route_retryable + int(outcome.route_retryable),
            retry_seconds=max(self.retry_seconds, blocked.wait_seconds if blocked else 0),
        )

    def raise_final(self, request: StructuredProviderRequest, provider_count: int) -> None:
        if route_bound(request) and self.route_retryable == provider_count:
            detail = self.last_error or self.blocked_error or "all_route_candidates_temporarily_unavailable"
            raise ProviderRouteDeferred(
                str(detail), retry_after_seconds=self.retry_seconds,
            ) from self.last_error
        if self.blocked_error is not None and (
            self.last_error is None or self.last_error is self.blocked_error
        ):
            raise self.blocked_error
        raise_provider_generation_failure(self.last_error, request.purpose)


def generate_structured_with_candidates(
    session: Session,
    provider: AiProvider,
    request: StructuredProviderRequest,
) -> tuple[object, int]:
    request = replace(request, system_prompt=with_automation_identity(request.system_prompt))
    providers, calls = structured_provider_calls(session, provider, request)
    failures = _StructuredFailures()
    for priority, (candidate, credentials) in enumerate(calls, 1):
        outcome = attempt_structured_candidate(
            session,
            candidate,
            credentials,
            request=request,
            priority=priority,
            model_name=route_model(request, candidate),
            has_more=candidate != providers[-1],
        )
        if outcome.result is not None:
            return outcome.result
        failures = failures.add(outcome)
        if not outcome.continue_candidates:
            break
    failures.raise_final(request, len(providers))
    raise RuntimeError("structured provider resolution returned without a result")


def structured_provider_calls(
    session: Session,
    provider: AiProvider,
    request: StructuredProviderRequest,
):
    providers = provider_candidates(
        session,
        provider,
        required_model_family=request.required_model_family,
        allow_quota_rotation=allow_quota_rotation(request),
        route_provider_ids=tuple(request.config.get("_ai_provider_route_provider_ids") or ()),
    )
    calls = provider_calls(
        session,
        providers,
        request.model_name,
        close_transaction_before_external=bool(
            request.config.get("_close_db_transaction_before_ai")
        ),
        route_models=route_models(request),
    )
    return providers, calls


def attempt_structured_candidate(
    session: Session,
    candidate: AiProvider,
    credentials: AiProviderCredentials,
    *,
    request: StructuredProviderRequest,
    priority: int,
    model_name: str,
    has_more: bool,
) -> StructuredAttemptOutcome:
    clock = ProviderAttemptClock.start()
    provider_request_id = _candidate_request_id(
        request, candidate.id, priority, model_name,
    )
    gateway, tracked_request = _tracked_structured_request(session, request, candidate=candidate,
        credentials=credentials, provider_request_id=provider_request_id)
    try:
        lease = begin_provider_call(candidate)
    except ProviderAdmissionBlocked as exc:
        return _structured_admission_blocked_outcome(
            session, candidate, request=request, priority=priority,
            model_name=model_name, clock=clock, error=exc, failover=True,
        )
    try:
        payload, usage = call_structured_provider(
            lease, credentials, tracked_request, provider_request_id=provider_request_id, gateway=gateway,
        )
    except ProviderAdmissionBlocked as exc:
        return _structured_admission_blocked_outcome(
            session, candidate, request=tracked_request, priority=priority,
            model_name=model_name, clock=clock, error=exc,
            failover=route_bound(request) and has_more,
        )
    except Exception as exc:
        return _structured_failed_outcome(
            session, candidate, request=tracked_request, priority=priority,
            model_name=model_name, clock=clock, error=exc, has_more=has_more,
        )
    record_attempt(
        session, tracked_request, candidate,
        priority=priority, model_name=model_name, clock=clock,
        outcome="success", usage=usage,
    )
    return StructuredAttemptOutcome(
        (payload, int(usage.total_tokens or 0)),
        None,
        False,
        False,
    )


def _tracked_structured_request(session, request, *, candidate, credentials, provider_request_id):
    gateway = scoped_provider_gateway(ai_gateway, session, config=request.config, provider_id=candidate.id,
        credentials=credentials, purpose=str(request.config.get("_ai_provider_route_purpose") or request.purpose),
        request_id=provider_request_id)
    tracked = replace(request, config={**request.config,
        "_provider_http_chain_id": getattr(gateway, "_provider_http_chain_id", "")})
    return gateway, tracked


def _structured_admission_blocked_outcome(
    session: Session,
    candidate: AiProvider,
    *,
    request: StructuredProviderRequest,
    priority: int,
    model_name: str,
    clock: ProviderAttemptClock,
    error: Exception,
    failover: bool,
) -> StructuredAttemptOutcome:
    record_attempt(
        session, request, candidate,
        priority=priority, model_name=model_name, clock=clock,
        outcome="admission_blocked", error=error,
    )
    return StructuredAttemptOutcome(None, error, route_bound(request), failover)


def _structured_failed_outcome(
    session: Session,
    candidate: AiProvider,
    *,
    request: StructuredProviderRequest,
    priority: int,
    model_name: str,
    clock: ProviderAttemptClock,
    error: Exception,
    has_more: bool,
) -> StructuredAttemptOutcome:
    record_attempt(
        session, request, candidate,
        priority=priority, model_name=model_name, clock=clock,
        outcome="failed", error=error, usage=getattr(error, "usage", None),
    )
    return structured_failure_outcome(
        session, candidate, request=request, error=error, has_more=has_more,
    )


def structured_failure_outcome(
    session: Session,
    candidate: AiProvider,
    *,
    request: StructuredProviderRequest,
    error: Exception,
    has_more: bool,
) -> StructuredAttemptOutcome:
    if isinstance(error, AiProviderResultUnknown):
        return StructuredAttemptOutcome(None, error, False, False)
    if is_ai_provider_quota_exhausted(error):
        mark_provider_quota_exhausted(candidate, error)
        if bool(request.config.get("_close_db_transaction_before_ai")):
            session.add(candidate)
            session.commit()
        return StructuredAttemptOutcome(None, error, route_bound(request), has_more)
    if route_bound(request) and route_transport_failure(error):
        return StructuredAttemptOutcome(None, error, True, True)
    return StructuredAttemptOutcome(None, error, False, False)


def record_attempt(
    session: Session,
    request: StructuredProviderRequest,
    candidate: AiProvider,
    *,
    priority: int,
    model_name: str,
    clock: ProviderAttemptClock,
    outcome: str,
    error: Exception | None = None,
    usage: AiUsage | None = None,
) -> None:
    record_provider_attempt(
        session,
        request.config,
        candidate,
        purpose=str(request.config.get("_ai_provider_route_purpose") or request.purpose),
        priority=priority,
        model_name=model_name,
        request_text=f"{request.system_prompt}\n{request.user_prompt}",
        provider_request_id=_candidate_request_id(
            request, candidate.id, priority, model_name,
        ),
        outcome="provider_result_unknown" if isinstance(error, AiProviderResultUnknown) else outcome,
        error_code=type(error).__name__ if error else "",
        latency_ms=clock.latency_ms(),
        usage=usage,
        http_chain_id=str(request.config.get("_provider_http_chain_id") or ""),
    )


def call_structured_provider(
    lease: ProviderProbeLease,
    credentials: AiProviderCredentials,
    request: StructuredProviderRequest,
    *,
    provider_request_id: str,
    gateway=None,
) -> tuple[object, AiUsage]:
    schema_purpose = str(
        request.config.get("_ai_provider_route_purpose") or request.purpose
    )
    antigravity = getattr(credentials, "provider_type", "") == "antigravity_cli"
    try:
        payload, usage = (gateway or ai_gateway).generate_structured(
            credentials,
            request.user_prompt,
            temperature=None if antigravity else request.temperature,
            max_tokens=None if antigravity else request.max_tokens,
            system_prompt=request.system_prompt,
            **provider_invocation_options(request.config, legacy_timeout=AI_CONTENT_REQUEST_TIMEOUT_SECONDS),
            request_id=provider_request_id,
            json_schema=antigravity_schema_for_purpose(schema_purpose, request.config),
        )
    except ProviderAdmissionBlocked:
        raise
    except Exception:
        release_provider_probe(lease)
        raise
    settle_provider_success(lease)
    return payload, usage


def _candidate_request_id(
    request: StructuredProviderRequest,
    provider_id: int,
    priority: int,
    model_name: str,
) -> str:
    request_id = request.request_id()
    if not request_id:
        return ""
    route_item = f"provider:{provider_id}:priority:{priority}:model:{model_name}"
    item_hash = hashlib.sha256(route_item.encode("utf-8")).hexdigest()[:20]
    return f"{request_id}:i{item_hash}"


def route_bound(request: StructuredProviderRequest) -> bool:
    return bool(request.config.get("_ai_provider_route_set_id"))


def route_models(request: StructuredProviderRequest) -> dict[int, str]:
    return {
        int(key): str(value)
        for key, value in dict(request.config.get("_ai_provider_route_models") or {}).items()
    }


def route_model(request: StructuredProviderRequest, provider: AiProvider) -> str:
    return route_models(request).get(provider.id, request.model_name)


def allow_quota_rotation(request: StructuredProviderRequest) -> bool:
    return not request.config.get("ai_provider_id") and request.stage in {"", "primary_default"}
