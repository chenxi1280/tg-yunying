from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models import (
    Action,
    AiContentPolicyVersion,
    AiContentWindowPlanSlot,
    GenerationJob,
    Task,
    TaskAiContentPolicyBinding,
)

from .ai_content_policy import assert_route_authorized
from .ai_content_runtime import (
    WindowScope,
    WindowSlotSpec,
    claim_window_slot,
    freeze_window_plan,
    invalidate_terminal_pre_gateway_obligation_slot,
)
from .ai_context_information import meaningful_group_evidence
from .ai_context_revision_binding import synchronize_generation_context
from .ai_generation_timing import GENERATION_LEASE
from .ai_generation_context_contract import freeze_generation_context_contract
from .ai_generation_policy_ownership import GenerationPolicySnapshot, generation_policy_snapshots
from .ai_negative_lexicon import enabled_negative_phrases
from .ai_provider_routes import route_v2_enabled
from .message_brief import fact_id_map


from .ai_content_job_binding_error import AiContentJobBindingError
from .ai_content_job_support import (
    due_at as _due_at,
    example_version as _example_version,
    generation_jobs_for_batch,
    registry_version as _registry_version,
    stable_hash as _hash,
)


@dataclass(frozen=True)
class _GroupContractRequest:
    action: Action
    job: GenerationJob
    scope_id: str
    evidence_lines: tuple[str, ...]
    route: str
    snapshot: GenerationPolicySnapshot


_ROUTE_MARKERS = {
    "adult_visual": ("身材", "腿长", "腿又长", "胸", "嘴唇", "写真", "性感", "黑丝", "丝袜", "高跟鞋"),
    "adult_product": ("成人用品", "情趣用品", "跳蛋", "飞机杯", "按摩棒"),
    "adult_service_inquiry": ("怎么约", "能约", "可约", "上门", "包夜", "讲课费"),
    "adult_service_sensory": ("好润", "真润", "够润", "水多不", "水多吗", "水滋滋", "湿不湿", "润不润"),
}
_ADULT_CONTEXT_MARKERS = tuple(
    dict.fromkeys(
        marker for markers in _ROUTE_MARKERS.values() for marker in markers
    )
)
_WINDOW_PERIOD_KEY_MAX_LENGTH = 80


def bind_group_generation_contracts(
    session: Session,
    task: Task,
    batch: list[tuple[Action, object]],
    *,
    config: dict,
    jobs: tuple[GenerationJob, ...] | None = None,
) -> dict:
    if not route_v2_enabled(config):
        return config
    bound_jobs = jobs or generation_jobs_for_batch(session, batch)
    if len(bound_jobs) != len(batch):
        raise AiContentJobBindingError("generation_job_batch_size_mismatch")
    snapshots = generation_policy_snapshots(session, task, bound_jobs)
    requests = _group_contract_requests(batch, bound_jobs, config=config, snapshots=snapshots)
    _authorize_group_requests(session, requests)
    contracts = {
        request.job.id: _bind_job_contract(
            session,
            task,
            request.action,
            job=request.job,
            binding=request.snapshot.binding,
            policy=request.snapshot.policy,
            scope_type="group",
            scope_id=request.scope_id,
            evidence_lines=request.evidence_lines,
            route=request.route,
            scope_authorized=True,
        )
        for request in requests
    }
    return {**config, "_ai_content_contracts": contracts}


def bind_comment_generation_contract(
    session: Session,
    task: Task,
    *,
    action: Action,
    payload,
    config: dict,
    job: GenerationJob,
) -> dict:
    if not route_v2_enabled(config):
        return config
    snapshot = generation_policy_snapshots(session, task, (job,))[job.id]
    evidence = (
        str(getattr(payload, "message_content", "") or ""),
        str(getattr(payload, "reply_target_preview", "") or ""),
    )
    route = snapshot.frozen_route or _context_route(config, snapshot.binding, evidence)
    contract = _bind_job_contract(
        session,
        task,
        action,
        job=job,
        binding=snapshot.binding,
        policy=snapshot.policy,
        scope_type="comment_source",
        scope_id=str(getattr(payload, "channel_target_id", "") or ""),
        evidence_lines=evidence,
        route=route,
    )
    return {**config, "_ai_content_contract": contract}


def enrich_group_generation_slots(
    config: dict,
    batch: list[tuple[Action, object]],
    slots: list[dict],
) -> list[dict]:
    contracts = dict(config.get("_ai_content_contracts") or {})
    if not contracts:
        return slots
    enriched = []
    for (_action, payload), slot in zip(batch, slots, strict=True):
        job_id = str(getattr(payload, "generation_job_id", "") or "")
        contract = dict(contracts.get(job_id) or {})
        if not contract:
            raise AiContentJobBindingError("generation_job_contract_missing")
        enriched.append({**slot, **contract, "generation_job_id": job_id})
    return enriched


def _bind_job_contract(
    session: Session, task: Task, action: Action, *,
    job: GenerationJob, binding: TaskAiContentPolicyBinding,
    policy: AiContentPolicyVersion, scope_type: str, scope_id: str,
    evidence_lines: tuple[str, ...], route: str,
    scope_authorized: bool = False,
) -> dict:
    if not scope_authorized:
        _assert_scope(session, binding, route=route, scope_type=scope_type, scope_id=scope_id)
    synchronize_generation_context(
        session,
        job,
        tenant_id=task.tenant_id,
        scope_type="group" if scope_type == "group" else "comment_source",
        scope_id=scope_id,
    )
    evidence = fact_id_map(evidence_lines)
    if not evidence:
        raise AiContentJobBindingError("context_route_evidence_missing")
    prompt_version = _registry_version(policy.prompt_registry, route, "prompt_contract")
    example_version = _example_version(policy.example_set)
    slot = _ensure_window_slot(
        session,
        task,
        action,
        job=job,
        scope_type="group" if scope_type == "group" else "comment_source",
        scope_id=scope_id,
        route=route,
        policy_hash=policy.policy_hash,
        prompt_version=prompt_version,
        evidence_hash=_hash({"facts": evidence, "context": job.context_snapshot_hash}),
        config_revision=binding.task_config_revision,
    )
    freeze_generation_context_contract(
        session, task, action, job=job, evidence=evidence, evidence_lines=evidence_lines,
        route=route, prompt_version=prompt_version, gate_config=dict(policy.gate_config or {}),
        config_revision=binding.task_config_revision,
    )
    _bind_job_policy(job, binding.evidence_hash, policy.policy_hash,
                     example_version=example_version)
    return _contract_payload(
        job,
        slot,
        evidence,
        policy=policy,
        example_version=example_version,
    )


def _group_contract_requests(
    batch: list[tuple[Action, object]],
    jobs: tuple[GenerationJob, ...],
    *,
    config: dict,
    snapshots: dict[str, GenerationPolicySnapshot],
) -> tuple[_GroupContractRequest, ...]:
    return tuple(
        _group_contract_request(action, payload, job, config=config, snapshot=snapshots[job.id])
        for (action, payload), job in zip(batch, jobs, strict=True)
    )


def _group_contract_request(
    action: Action,
    payload: object,
    job: GenerationJob,
    *,
    config: dict,
    snapshot: GenerationPolicySnapshot,
) -> _GroupContractRequest:
    evidence = meaningful_group_evidence(
        str(getattr(payload, "ai_generation_history", "") or ""),
        getattr(payload, "topic_direction", {}), _ADULT_CONTEXT_MARKERS,
    )
    return _GroupContractRequest(
        action=action,
        job=job,
        scope_id=str(getattr(payload, "group_id", "") or ""),
        evidence_lines=evidence,
        route=snapshot.frozen_route or _context_route(
            config,
            snapshot.binding,
            evidence,
            selector=int(action.pacing_slot_ordinal or job.generation_sequence or 0),
        ),
        snapshot=snapshot,
    )


def _authorize_group_requests(
    session: Session,
    requests: tuple[_GroupContractRequest, ...],
) -> None:
    scopes = {
        (request.snapshot.binding.id, request.route, request.scope_id): request.snapshot.binding
        for request in requests
    }
    for (_binding_id, route, scope_id), binding in scopes.items():
        _assert_scope(
            session,
            binding,
            route=route,
            scope_type="group",
            scope_id=scope_id,
        )


def _context_route(
    config: dict,
    binding: TaskAiContentPolicyBinding,
    evidence_lines: tuple[str, ...],
    *,
    selector: int = 0,
) -> str:
    explicit = str(config.get("ai_content_context_route") or "").strip()
    allowed = tuple(str(item) for item in (binding.allowed_routes or ()))
    if explicit:
        return explicit
    evidence = " ".join(str(item or "") for item in evidence_lines)
    matched = tuple(
        route for route, markers in _ROUTE_MARKERS.items()
        if route in allowed and any(marker in evidence for marker in markers)
    )
    if matched:
        return matched[selector % len(matched)]
    if not matched and "general" in allowed:
        return "general"
    raise AiContentJobBindingError("context_route_unproven")


def _assert_scope(
    session: Session,
    binding: TaskAiContentPolicyBinding,
    *,
    route: str,
    scope_type: str,
    scope_id: str,
) -> None:
    if not scope_id:
        raise AiContentJobBindingError("content_route_scope_missing")
    assert_route_authorized(
        session,
        binding,
        route=route,
        scope_type="task_group" if scope_type == "group" else "task_source",
        scope_id=scope_id,
    )


def _ensure_window_slot(
    session: Session,
    task: Task,
    action: Action,
    *,
    job: GenerationJob,
    scope_type: str,
    scope_id: str,
    route: str,
    policy_hash: str,
    prompt_version: str,
    evidence_hash: str,
    config_revision: int,
) -> AiContentWindowPlanSlot:
    if job.window_slot_id:
        slot = session.get(AiContentWindowPlanSlot, job.window_slot_id)
        if slot is None or slot.claimed_by_job_id != job.id:
            raise AiContentJobBindingError("generation_window_binding_invalid")
        return slot
    due_at = _due_at(action)
    pacing_hash = str(action.pacing_plan_hash or "")
    if not pacing_hash:
        raise AiContentJobBindingError("generation_window_pacing_plan_missing")
    scope = _window_scope(
        task,
        action,
        scope_type,
        scope_id=scope_id,
        due_at=due_at,
        pacing_hash=pacing_hash,
        policy_hash=policy_hash,
        context_revision=job.context_snapshot_version,
        generation_sequence=job.generation_sequence,
        config_revision=config_revision,
    )
    spec = _window_slot_spec(
        action,
        job,
        due_at,
        route=route,
        prompt_version=prompt_version,
        evidence_hash=evidence_hash,
    )
    invalidate_terminal_pre_gateway_obligation_slot(
        session,
        obligation_type=job.obligation_type,
        obligation_id=job.obligation_id,
    )
    freeze_window_plan(session, scope, (spec,))
    return claim_window_slot(session, job, lease_duration=GENERATION_LEASE)


def _window_scope(
    task: Task,
    action: Action,
    scope_type: str,
    *,
    scope_id: str,
    due_at: datetime,
    pacing_hash: str,
    policy_hash: str,
    context_revision: int,
    generation_sequence: int,
    config_revision: int,
) -> WindowScope:
    return WindowScope(
        tenant_id=task.tenant_id,
        task_id=task.id,
        task_lifecycle_epoch=int(task.task_lifecycle_epoch or 1),
        scope_type=scope_type,
        scope_id=scope_id,
        pacing_plan_hash=pacing_hash,
        period_key=_window_period_key(
            due_at,
            obligation_id=action.obligation_id,
            context_revision=context_revision,
            generation_sequence=generation_sequence,
        ),
        window_start_at=due_at,
        window_end_at=due_at + timedelta(hours=1),
        task_config_revision=config_revision,
        content_policy_hash=policy_hash,
    )


def _window_period_key(
    due_at: datetime,
    *,
    obligation_id: str,
    context_revision: int,
    generation_sequence: int,
) -> str:
    day = due_at.date().isoformat()
    identity = f"{obligation_id}:{context_revision}:{generation_sequence}"
    period_key = f"{day}:{identity}"
    if len(period_key) <= _WINDOW_PERIOD_KEY_MAX_LENGTH:
        return period_key
    return f"{day}:{_hash({'window_revision': identity})}"


def _window_slot_spec(
    action: Action,
    job: GenerationJob,
    due_at: datetime,
    *,
    route: str,
    prompt_version: str,
    evidence_hash: str,
) -> WindowSlotSpec:
    if not action.account_id:
        raise AiContentJobBindingError("generation_window_account_missing")
    return WindowSlotSpec(
        slot_ordinal=int(action.pacing_slot_ordinal or 1),
        obligation_type=job.obligation_type,
        obligation_id=job.obligation_id,
        generation_sequence=job.generation_sequence,
        account_id=int(action.account_id),
        due_at=due_at,
        context_scope_revision=job.context_snapshot_version,
        context_snapshot_hash=job.context_snapshot_hash,
        context_route=route,
        content_mode=route,
        route_evidence_hash=evidence_hash,
        prompt_contract_version=prompt_version,
    )


def _bind_job_policy(
    job: GenerationJob,
    binding_hash: str,
    policy_hash: str,
    *,
    example_version: str,
) -> None:
    job.task_binding_hash = binding_hash
    job.task_direction_snapshot_hash = binding_hash
    job.content_policy_hash = policy_hash
    job.example_set_version = example_version


def _contract_payload(
    job: GenerationJob,
    slot: AiContentWindowPlanSlot,
    evidence: dict[str, str],
    *,
    policy: AiContentPolicyVersion,
    example_version: str,
) -> dict:
    return {
        "task_direction_snapshot_hash": job.task_direction_snapshot_hash,
        "content_policy_hash": job.content_policy_hash,
        "window_plan_hash": job.window_plan_hash,
        "context_route": slot.context_route,
        "content_mode": slot.content_mode,
        "route_evidence_ids": list(evidence),
        "prompt_contract_version": slot.prompt_contract_version,
        "example_set_version": example_version,
        "forbidden_claim_categories": list(
            dict(policy.gate_config or {}).get("forbidden_claim_categories") or ()
        ),
        "negative_phrases": list(enabled_negative_phrases(
            dict(policy.gate_config or {}), slot.context_route,
        )),
    }


__all__ = [
    "AiContentJobBindingError",
    "bind_comment_generation_contract",
    "bind_group_generation_contracts",
    "enrich_group_generation_slots",
    "generation_jobs_for_batch",
]
