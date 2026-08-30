from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai_gateway import canonical_ai_model_identity
from app.models import (
    AiProvider,
    AiProviderHealthStatus,
    GenerationJob,
    TenantAiProviderRouteItem,
    TenantAiProviderRouteSet,
)


V2_ROUTE_FLAG = "ai_content_route_v2_enabled"
ROUTE_SNAPSHOTS_KEY = "_ai_provider_route_snapshots"
GROUP_ROUTE_PURPOSE = "group_context_route"
GROUP_REVIEW_PURPOSE = "group_semantic_review"
COMMENT_ROUTE_PURPOSE = "comment_context_route"
COMMENT_REALIZE_PURPOSE = "comment_realize_general"
COMMENT_REVIEW_PURPOSE = "comment_semantic_review"
REALIZE_PURPOSE_BY_MODE = {
    "general": "group_realize_general",
    "adult_visual": "group_realize_adult_visual",
    "adult_product": "group_realize_adult_product",
    "adult_service_inquiry": "group_realize_adult_service_inquiry",
    "adult_service_sensory": "group_realize_adult_service_sensory",
}
ALLOWED_PURPOSES = frozenset({
    GROUP_ROUTE_PURPOSE,
    GROUP_REVIEW_PURPOSE,
    COMMENT_ROUTE_PURPOSE,
    COMMENT_REALIZE_PURPOSE,
    COMMENT_REVIEW_PURPOSE,
    "offline_pairwise_eval",
    *REALIZE_PURPOSE_BY_MODE.values(),
})
ANTIGRAVITY_GENERATION_PURPOSES = frozenset({
    GROUP_ROUTE_PURPOSE,
    *REALIZE_PURPOSE_BY_MODE.values(),
})
ANTIGRAVITY_GENERATION_PURPOSE_ORDER = (
    GROUP_ROUTE_PURPOSE,
    "group_realize_general",
    "group_realize_adult_visual",
    "group_realize_adult_product",
    "group_realize_adult_service_inquiry",
    "group_realize_adult_service_sensory",
)


class ProviderRouteUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderRouteCandidate:
    provider: AiProvider
    priority: int
    model_name: str


@dataclass(frozen=True)
class ProviderRouteSnapshot:
    route_set_id: str
    revision: int
    content_hash: str
    purpose: str
    candidates: tuple[ProviderRouteCandidate, ...]

    @property
    def provider_ids(self) -> tuple[int, ...]:
        return tuple(item.provider.id for item in self.candidates)

    @property
    def provider_models(self) -> dict[int, str]:
        return {item.provider.id: item.model_name for item in self.candidates}


def route_v2_enabled(config: dict | None) -> bool:
    return bool((config or {}).get(V2_ROUTE_FLAG))


def request_route_purpose(request_purpose: str, config: dict) -> str:
    is_comment = str(config.get("_ai_content_scope_type") or "") == "comment"
    if request_purpose == "两阶段意图规划":
        return COMMENT_ROUTE_PURPOSE if is_comment else GROUP_ROUTE_PURPOSE
    if request_purpose == "两阶段语义审核":
        return COMMENT_REVIEW_PURPOSE if is_comment else GROUP_REVIEW_PURPOSE
    if request_purpose != "两阶段声线实现":
        raise ProviderRouteUnavailable(f"provider_route_purpose_unmapped:{request_purpose}")
    if is_comment:
        return COMMENT_REALIZE_PURPOSE
    mode = str(config.get("_ai_content_mode") or "general")
    purpose = REALIZE_PURPOSE_BY_MODE.get(mode)
    if purpose is None:
        raise ProviderRouteUnavailable(f"provider_route_mode_unmapped:{mode}")
    return purpose


def active_route_snapshot(
    session: Session,
    tenant_id: int,
    purpose: str,
) -> ProviderRouteSnapshot:
    if purpose not in ALLOWED_PURPOSES:
        raise ProviderRouteUnavailable(f"provider_route_purpose_invalid:{purpose}")
    route_set = session.scalar(select(TenantAiProviderRouteSet).where(
        TenantAiProviderRouteSet.tenant_id == tenant_id,
        TenantAiProviderRouteSet.purpose == purpose,
        TenantAiProviderRouteSet.status == "active",
    ))
    if route_set is None:
        raise ProviderRouteUnavailable(f"provider_route_set_missing:{purpose}")
    candidates = _enabled_candidates(session, route_set.id)
    if not candidates:
        raise ProviderRouteUnavailable(f"provider_route_candidates_empty:{purpose}")
    return ProviderRouteSnapshot(
        route_set.id,
        route_set.revision,
        route_set.content_hash,
        purpose,
        tuple(candidates),
    )


def resolve_request_route(
    session: Session,
    tenant_id: int,
    request_purpose: str,
    *,
    config: dict,
) -> ProviderRouteSnapshot | None:
    if not route_v2_enabled(config):
        return None
    purpose = request_route_purpose(request_purpose, config)
    frozen = dict(config.get(ROUTE_SNAPSHOTS_KEY) or {})
    if frozen:
        return _frozen_route_snapshot(session, purpose, frozen)
    if config.get("_generation_job_id"):
        raise ProviderRouteUnavailable(f"provider_route_snapshot_missing:{purpose}")
    snapshot = active_route_snapshot(session, tenant_id, purpose)
    if purpose in {GROUP_REVIEW_PURPOSE, COMMENT_REVIEW_PURPOSE}:
        _assert_reviewer_separation(session, tenant_id, snapshot, comment=purpose == COMMENT_REVIEW_PURPOSE)
    return snapshot


def bind_generation_job_routes(
    session: Session,
    jobs: tuple[GenerationJob, ...],
    config: dict,
    *,
    scope_type: str,
) -> dict:
    if not route_v2_enabled(config):
        return config
    if not jobs:
        raise ProviderRouteUnavailable("generation_job_missing_for_route_binding")
    modes = _bound_content_modes(config)
    snapshots = _batch_route_snapshots(session, jobs, scope_type, content_modes=modes)
    primary_purpose = _scope_purposes(scope_type, modes)[0]
    for job in jobs:
        _bind_job_route_snapshots(job, snapshots, primary_purpose=primary_purpose)
    return {
        **config,
        "_ai_content_scope_type": scope_type,
        "_generation_job_id": jobs[0].id,
        ROUTE_SNAPSHOTS_KEY: snapshots,
    }


def _batch_route_snapshots(
    session: Session,
    jobs: tuple[GenerationJob, ...],
    scope_type: str,
    *,
    content_modes: tuple[str, ...],
) -> dict:
    tenant_ids = {job.tenant_id for job in jobs}
    if len(tenant_ids) != 1:
        raise ProviderRouteUnavailable("generation_batch_tenant_mismatch")
    frozen = [dict(job.provider_route_snapshots or {}) for job in jobs if job.provider_route_snapshots]
    snapshots = frozen[0] if frozen else _active_scope_snapshots(
        session,
        tenant_ids.pop(),
        scope_type,
        content_modes=content_modes,
    )
    _validate_frozen_purposes(snapshots, scope_type, content_modes)
    if any(current != snapshots for current in frozen[1:]):
        raise ProviderRouteUnavailable("generation_batch_route_snapshot_mismatch")
    _assert_frozen_reviewer_separation(
        session,
        snapshots,
        scope_type,
        content_modes=content_modes,
    )
    return snapshots


def _active_scope_snapshots(
    session: Session,
    tenant_id: int,
    scope_type: str,
    *,
    content_modes: tuple[str, ...],
) -> dict:
    purposes = _scope_purposes(scope_type, content_modes)
    return {
        purpose: _serialize_snapshot(active_route_snapshot(session, tenant_id, purpose))
        for purpose in purposes
    }


def _bind_job_route_snapshots(
    job: GenerationJob,
    snapshots: dict,
    *,
    primary_purpose: str,
) -> None:
    current = dict(job.provider_route_snapshots or {})
    if current:
        if current != snapshots:
            raise ProviderRouteUnavailable("generation_batch_route_snapshot_mismatch")
        return
    primary = snapshots[primary_purpose]
    job.provider_route_snapshots = snapshots
    job.provider_route_set_id = str(primary["route_set_id"])
    job.provider_route_set_revision = int(primary["revision"])
    job.provider_route_set_hash = str(primary["content_hash"])


def _scope_purposes(
    scope_type: str,
    content_modes: tuple[str, ...] = (),
) -> tuple[str, ...]:
    if scope_type == "comment":
        return (COMMENT_ROUTE_PURPOSE, COMMENT_REALIZE_PURPOSE, COMMENT_REVIEW_PURPOSE)
    if scope_type == "group":
        modes = content_modes or tuple(REALIZE_PURPOSE_BY_MODE)
        try:
            realizers = tuple(dict.fromkeys(REALIZE_PURPOSE_BY_MODE[mode] for mode in modes))
        except KeyError as exc:
            raise ProviderRouteUnavailable(f"provider_route_mode_unmapped:{exc.args[0]}") from exc
        return (GROUP_ROUTE_PURPOSE, *realizers, GROUP_REVIEW_PURPOSE)
    raise ProviderRouteUnavailable(f"provider_route_scope_invalid:{scope_type}")


def _validate_frozen_purposes(
    frozen: dict,
    scope_type: str,
    content_modes: tuple[str, ...],
) -> None:
    missing = set(_scope_purposes(scope_type, content_modes)) - set(frozen)
    if missing:
        names = ",".join(sorted(missing))
        raise ProviderRouteUnavailable(f"provider_route_snapshot_incomplete:{names}")


def _serialize_snapshot(snapshot: ProviderRouteSnapshot) -> dict:
    return {
        "route_set_id": snapshot.route_set_id,
        "revision": snapshot.revision,
        "content_hash": snapshot.content_hash,
        "purpose": snapshot.purpose,
        "candidates": [
            {
                "provider_id": item.provider.id,
                "priority": item.priority,
                "model_name": item.model_name,
            }
            for item in snapshot.candidates
        ],
    }


def _frozen_route_snapshot(
    session: Session,
    purpose: str,
    frozen: dict,
) -> ProviderRouteSnapshot:
    payload = dict(frozen.get(purpose) or {})
    if not payload:
        raise ProviderRouteUnavailable(f"provider_route_snapshot_missing:{purpose}")
    candidates = _frozen_candidates(session, tuple(payload.get("candidates") or ()))
    if not candidates:
        raise ProviderRouteUnavailable(f"provider_route_candidates_empty:{purpose}")
    return ProviderRouteSnapshot(
        str(payload.get("route_set_id") or ""),
        int(payload.get("revision") or 0),
        str(payload.get("content_hash") or ""),
        purpose,
        candidates,
    )


def _frozen_candidates(
    session: Session,
    payloads: tuple[dict, ...],
) -> tuple[ProviderRouteCandidate, ...]:
    provider_ids = tuple(int(item.get("provider_id") or 0) for item in payloads)
    providers = session.scalars(select(AiProvider).where(AiProvider.id.in_(provider_ids))).all()
    by_id = {provider.id: provider for provider in providers}
    try:
        return tuple(
            ProviderRouteCandidate(
                by_id[int(item["provider_id"])],
                int(item["priority"]),
                str(item["model_name"]),
            )
            for item in payloads
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProviderRouteUnavailable("provider_route_snapshot_invalid") from exc


def _assert_frozen_reviewer_separation(
    session: Session,
    snapshots: dict,
    scope_type: str,
    *,
    content_modes: tuple[str, ...],
) -> None:
    review_purpose = COMMENT_REVIEW_PURPOSE if scope_type == "comment" else GROUP_REVIEW_PURPOSE
    reviewer = _frozen_route_snapshot(session, review_purpose, snapshots)
    generator_purposes = tuple(
        purpose for purpose in _scope_purposes(scope_type, content_modes)
        if purpose != review_purpose
    )
    generators = {
        _identity(candidate)
        for purpose in generator_purposes
        for candidate in _frozen_route_snapshot(session, purpose, snapshots).candidates
    }
    if generators & {_identity(item) for item in reviewer.candidates}:
        raise ProviderRouteUnavailable("semantic_reviewer_must_differ_from_all_generators")


def _bound_content_modes(config: dict) -> tuple[str, ...]:
    contracts = [
        *dict(config.get("_ai_content_contracts") or {}).values(),
        dict(config.get("_ai_content_contract") or {}),
    ]
    return tuple(dict.fromkeys(
        str(contract.get("content_mode") or "")
        for contract in contracts
        if contract.get("content_mode")
    ))


def route_config(config: dict, snapshot: ProviderRouteSnapshot) -> dict:
    return {
        **config,
        "_ai_provider_route_set_id": snapshot.route_set_id,
        "_ai_provider_route_set_revision": snapshot.revision,
        "_ai_provider_route_set_hash": snapshot.content_hash,
        "_ai_provider_route_purpose": snapshot.purpose,
        "_ai_provider_route_provider_ids": list(snapshot.provider_ids),
        "_ai_provider_route_models": snapshot.provider_models,
    }


def _enabled_candidates(session: Session, route_set_id: str) -> list[ProviderRouteCandidate]:
    rows = session.execute(
        select(TenantAiProviderRouteItem, AiProvider)
        .join(AiProvider, AiProvider.id == TenantAiProviderRouteItem.provider_id)
        .where(
            TenantAiProviderRouteItem.route_set_id == route_set_id,
            TenantAiProviderRouteItem.enabled.is_(True),
            AiProvider.credential_enabled.is_(True),
            AiProvider.health_status == AiProviderHealthStatus.HEALTHY.value,
        )
        .order_by(TenantAiProviderRouteItem.priority)
    )
    return [
        ProviderRouteCandidate(provider, item.priority, item.model_name)
        for item, provider in rows
    ]


def _assert_reviewer_separation(
    session: Session,
    tenant_id: int,
    reviewer: ProviderRouteSnapshot,
    *,
    comment: bool,
) -> None:
    purposes = (COMMENT_ROUTE_PURPOSE, COMMENT_REALIZE_PURPOSE) if comment else (
        GROUP_ROUTE_PURPOSE,
        *REALIZE_PURPOSE_BY_MODE.values(),
    )
    generator_ids = _active_identities(session, tenant_id, purposes)
    reviewer_ids = {_identity(item) for item in reviewer.candidates}
    if generator_ids & reviewer_ids:
        raise ProviderRouteUnavailable("semantic_reviewer_must_differ_from_all_generators")


def _active_identities(
    session: Session,
    tenant_id: int,
    purposes: tuple[str, ...],
) -> set[str]:
    identities: set[str] = set()
    for purpose in purposes:
        try:
            snapshot = active_route_snapshot(session, tenant_id, purpose)
        except ProviderRouteUnavailable:
            continue
        identities.update(_identity(item) for item in snapshot.candidates)
    return identities


def _identity(candidate: ProviderRouteCandidate) -> str:
    model = canonical_ai_model_identity(candidate.model_name)
    return f"{candidate.provider.id}:{model}"


__all__ = [
    "ALLOWED_PURPOSES",
    "ProviderRouteSnapshot",
    "ProviderRouteUnavailable",
    "ROUTE_SNAPSHOTS_KEY",
    "V2_ROUTE_FLAG",
    "active_route_snapshot",
    "bind_generation_job_routes",
    "request_route_purpose",
    "resolve_request_route",
    "route_config",
    "route_v2_enabled",
]
