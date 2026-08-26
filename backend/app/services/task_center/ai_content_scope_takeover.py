from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    AiContentScopeTakeoverBatch,
    AiContentScopeTakeoverItem,
    ContentMixCycle,
    ContentMixCycleSlot,
    ExecutionAttempt,
    Task,
    TaskAccountDailyCoverage,
    TaskGroupDailyMessageSlot,
)

from .group_ai_scope import (
    CONTENT_SCOPE_CONTRACT_VERSION,
    validate_group_ai_content_scope,
)
from .ai_content_scope_takeover_context import (
    TakeoverClassificationContext,
    build_takeover_classification_context,
)
from .ai_content_scope_takeover_facts import classification_facts
from .fulfillment_activation import CURRENT_CONTRACT_VERSION
from .payloads import SendMessagePayload
from .runtime_state_hash import (
    action_state_hash,
    canonical_state_hash,
)


OPEN_STATUSES = frozenset({"pending", "claiming", "executing", "retryable_failed"})
TAKEOVER_CANDIDATE_STATUSES = tuple(sorted(OPEN_STATUSES | {"unknown_after_send"}))
SCOPE_KEYS = (
    "content_scope_contract_version",
    "content_scope_tenant_id",
    "content_scope_group_id",
    "content_scope_task_id",
)


@dataclass(frozen=True)
class TakeoverClassification:
    name: str
    input_hash: str
    reason_code: str


def preview_ai_content_scope_takeover(
    session: Session,
    *,
    cutoff_at: datetime,
    actor: str,
    dispatcher_scope: str,
    release_version: str,
    config_version: str,
    supersedes_batch_id: str | None = None,
) -> AiContentScopeTakeoverBatch:
    actions = _preview_actions(session, cutoff_at, supersedes_batch_id)
    context = build_takeover_classification_context(session, actions)
    classified = [
        (action, classify_takeover_action(session, action, context=context))
        for action in actions
    ]
    item_facts = [_item_fact(action, result) for action, result in classified]
    batch = AiContentScopeTakeoverBatch(
        dispatcher_scope=dispatcher_scope,
        cutoff_at=cutoff_at,
        actor=actor,
        classification_hash=canonical_state_hash(item_facts),
        classification_counts=dict(Counter(row.name for _, row in classified)),
        supersedes_batch_id=supersedes_batch_id,
        release_version=release_version,
        config_version=config_version,
    )
    session.add(batch)
    session.flush()
    session.add_all([
        AiContentScopeTakeoverItem(
            batch_id=batch.id,
            action_id=action.id,
            observed_action_state_hash=action_state_hash(action),
            classification=result.name,
            classification_input_hash=result.input_hash,
            outcome={"reason_code": result.reason_code},
        )
        for action, result in classified
    ])
    session.flush()
    return batch


def classify_takeover_action(
    session: Session,
    action: Action,
    *,
    context: TakeoverClassificationContext | None = None,
) -> TakeoverClassification:
    attempt = _latest_attempt(session, action.id, context)
    facts_hash = _classification_hash(session, action, attempt, context)
    if action.status == "unknown_after_send" or _gateway_started_open(action, attempt):
        return TakeoverClassification(
            "remote_reconcile_required", facts_hash, "gateway_boundary_exists",
        )
    if action.status not in OPEN_STATUSES:
        return TakeoverClassification(
            "immutable_terminal", facts_hash, "terminal_action_immutable",
        )
    payload = _parsed_payload(action)
    if payload is None:
        return TakeoverClassification(
            "replan_required", facts_hash, "legacy_payload_invalid_pre_gateway",
        )
    binding = _binding_classification(session, action, payload, context)
    if binding is not None:
        name, reason = binding
        return TakeoverClassification(name, facts_hash, reason)
    return _scope_classification(session, action, payload, facts_hash, context)


def _classification_hash(
    session: Session,
    action: Action,
    attempt: ExecutionAttempt | None,
    context: TakeoverClassificationContext | None,
) -> str:
    quantity = _quantity(
        session, str(action.primary_quantity_slot_id or ""), context,
    )
    facts = classification_facts(
        session,
        action,
        attempt,
        task=_task(session, action, context),
        binding_rows=_binding_rows(session, action, context),
        quantity=quantity,
        coverage=_coverage(session, quantity, context),
        context=context,
    )
    return canonical_state_hash(facts)


def _scope_classification(
    session: Session,
    action: Action,
    payload: SendMessagePayload,
    facts_hash: str,
    context: TakeoverClassificationContext | None,
) -> TakeoverClassification:
    scope_values = [getattr(payload, key) for key in SCOPE_KEYS]
    if all(scope_values):
        violation = validate_group_ai_content_scope(
            session, action, payload=payload, account_id=action.account_id,
            facts=context.scope_facts if context else None,
        )
        if violation is None:
            return TakeoverClassification(
                "already_current", facts_hash, "scope_contract_current",
            )
        return TakeoverClassification("quarantine", facts_hash, violation.code)
    if any(scope_values):
        return TakeoverClassification(
            "quarantine", facts_hash, "partial_scope_contract",
        )
    candidate = payload.model_copy(update=_scope_snapshot(action, payload))
    violation = validate_group_ai_content_scope(
        session, action, payload=candidate, account_id=action.account_id,
        facts=context.scope_facts if context else None,
    )
    if violation is None:
        return TakeoverClassification(
            "equivalent_snapshot_safe", facts_hash, "equivalent_scope_proven",
        )
    severity = _scope_violation_severity(violation.field)
    return TakeoverClassification(severity, facts_hash, violation.code)


def scope_snapshot_payload(action: Action) -> dict:
    payload = _parsed_payload(action)
    if payload is None:
        raise ValueError("legacy_payload_invalid")
    updated = dict(action.payload or {})
    updated.update(_scope_snapshot(action, payload))
    return updated


def recompute_takeover_hashes(
    session: Session,
    action: Action,
    *,
    context: TakeoverClassificationContext | None = None,
) -> tuple[str, TakeoverClassification]:
    return action_state_hash(action), classify_takeover_action(
        session, action, context=context,
    )


def takeover_classification_reason_counts(
    session: Session,
    batch_id: str,
) -> dict[str, int]:
    rows = session.execute(select(
        AiContentScopeTakeoverItem.classification,
        AiContentScopeTakeoverItem.outcome,
    ).where(AiContentScopeTakeoverItem.batch_id == batch_id)).all()
    counts = Counter(
        f"{classification}:{(outcome or {}).get('reason_code') or 'unknown'}"
        for classification, outcome in rows
    )
    return dict(sorted(counts.items()))


def _preview_actions(
    session: Session,
    cutoff_at: datetime,
    supersedes_batch_id: str | None,
) -> list[Action]:
    if supersedes_batch_id:
        ids = _superseded_action_ids(session, supersedes_batch_id)
        if not ids:
            return []
        statement = select(Action).where(Action.id.in_(ids))
    else:
        statement = select(Action).join(Task, Task.id == Action.task_id).where(
            Action.task_type == "group_ai_chat",
            Action.action_type == "send_message",
            Action.status.in_(TAKEOVER_CANDIDATE_STATUSES),
            Action.created_at <= cutoff_at,
            Task.status == "running",
        )
    return list(session.scalars(statement.order_by(Action.id.asc())))


def _superseded_action_ids(session: Session, batch_id: str) -> list[str]:
    batch = session.get(AiContentScopeTakeoverBatch, batch_id)
    if batch is None or batch.status not in {"blocked", "applying"}:
        raise ValueError("takeover_superseded_batch_invalid")
    return list(session.scalars(
        select(AiContentScopeTakeoverItem.action_id).where(
            AiContentScopeTakeoverItem.batch_id == batch_id,
            AiContentScopeTakeoverItem.status.in_(
                ("pending", "conflict", "quarantined"),
            ),
        ).order_by(AiContentScopeTakeoverItem.action_id.asc())
    ))


def _item_fact(action: Action, result: TakeoverClassification) -> dict:
    return {
        "action_id": action.id,
        "action_state_hash": action_state_hash(action),
        "classification": result.name,
        "classification_input_hash": result.input_hash,
    }


def _parsed_payload(action: Action) -> SendMessagePayload | None:
    try:
        return SendMessagePayload.model_validate(action.payload or {})
    except (ValidationError, ValueError, TypeError):
        return None


def _binding_classification(
    session: Session,
    action: Action,
    payload: SendMessagePayload,
    context: TakeoverClassificationContext | None,
) -> tuple[str, str] | None:
    task = _task(session, action, context)
    if _uses_fact_first_binding(task, action, payload):
        return _fact_first_binding_classification(session, action, payload, context)
    ids = (
        str(action.content_mix_cycle_slot_id or ""),
        str(action.primary_quantity_slot_id or ""),
        str(payload.content_mix_cycle_slot_id or ""),
        str(payload.primary_quantity_slot_id or ""),
    )
    if not all(ids):
        return "replan_required", "content_mix_binding_missing"
    if len(set((ids[0], ids[2]))) != 1 or len(set((ids[1], ids[3]))) != 1:
        return "quarantine", "content_mix_binding_conflict"
    rows = _binding_rows(session, action, context)
    if rows is None:
        return "replan_required", "content_mix_binding_fact_missing"
    cycle_slot, quantity, cycle = rows
    if _binding_ownership_conflicts(
        action,
        cycle_slot=cycle_slot,
        quantity=quantity,
        cycle=cycle,
    ):
        return "quarantine", "content_mix_binding_ownership_conflict"
    return _coverage_binding_classification(
        session,
        action,
        payload=payload,
        quantity=quantity,
        context=context,
    )


def _task(
    session: Session,
    action: Action,
    context: TakeoverClassificationContext | None,
) -> Task | None:
    if not action.task_id:
        return None
    if context:
        return context.scope_facts.tasks.get(action.task_id)
    return session.get(Task, action.task_id)


def _uses_fact_first_binding(
    task: Task | None,
    action: Action,
    payload: SendMessagePayload,
) -> bool:
    return bool(
        task
        and task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION
        and not action.content_mix_cycle_slot_id
        and not payload.content_mix_cycle_slot_id
    )


def _fact_first_binding_classification(
    session: Session,
    action: Action,
    payload: SendMessagePayload,
    context: TakeoverClassificationContext | None,
) -> tuple[str, str] | None:
    action_id = str(action.primary_quantity_slot_id or "")
    payload_id = str(payload.primary_quantity_slot_id or "")
    if not action_id or not payload_id:
        return "replan_required", "fact_first_quantity_binding_missing"
    if action_id != payload_id:
        return "quarantine", "fact_first_quantity_binding_conflict"
    quantity = _quantity(session, action_id, context)
    if quantity is None:
        return "replan_required", "fact_first_quantity_fact_missing"
    if quantity.task_id != action.task_id or quantity.tenant_id != action.tenant_id:
        return "quarantine", "fact_first_quantity_ownership_conflict"
    return _coverage_binding_classification(
        session,
        action,
        payload=payload,
        quantity=quantity,
        context=context,
    )


def _quantity(
    session: Session,
    quantity_id: str,
    context: TakeoverClassificationContext | None,
) -> TaskGroupDailyMessageSlot | None:
    if context:
        return context.quantities.get(quantity_id)
    return session.get(TaskGroupDailyMessageSlot, quantity_id)


def _coverage(
    session: Session,
    quantity: TaskGroupDailyMessageSlot | None,
    context: TakeoverClassificationContext | None,
) -> TaskAccountDailyCoverage | None:
    coverage_id = str(
        quantity.task_account_daily_coverage_id or ""
    ) if quantity else ""
    if not coverage_id:
        return None
    if context:
        return context.coverages.get(coverage_id)
    return session.get(TaskAccountDailyCoverage, coverage_id)


def _binding_rows(
    session: Session,
    action: Action,
    context: TakeoverClassificationContext | None = None,
) -> tuple[ContentMixCycleSlot, TaskGroupDailyMessageSlot, ContentMixCycle] | None:
    cycle_slot_id = str(action.content_mix_cycle_slot_id or "")
    quantity_id = str(action.primary_quantity_slot_id or "")
    if not cycle_slot_id or not quantity_id:
        return None
    if context:
        cycle_slot = context.cycle_slots.get(cycle_slot_id)
        quantity = context.quantities.get(quantity_id)
        cycle = context.cycles.get(cycle_slot.cycle_id) if cycle_slot else None
    else:
        cycle_slot = session.get(ContentMixCycleSlot, cycle_slot_id)
        quantity = session.get(TaskGroupDailyMessageSlot, quantity_id)
        cycle = session.get(ContentMixCycle, cycle_slot.cycle_id) if cycle_slot else None
    if cycle_slot is None or quantity is None or cycle is None:
        return None
    return cycle_slot, quantity, cycle


def _binding_ownership_conflicts(
    action,
    *,
    cycle_slot,
    quantity,
    cycle,
) -> bool:
    return bool(
        cycle_slot.primary_quantity_slot_id != quantity.id
        or cycle_slot.current_action_id != action.id
        or quantity.task_id != action.task_id
        or quantity.tenant_id != action.tenant_id
        or cycle.task_id != action.task_id
        or cycle.tenant_id != action.tenant_id
        or cycle.task_day_ledger_id != quantity.task_day_ledger_id
    )


def _coverage_binding_classification(
    session: Session,
    action: Action,
    *,
    payload: SendMessagePayload,
    quantity: TaskGroupDailyMessageSlot,
    context: TakeoverClassificationContext | None = None,
) -> tuple[str, str] | None:
    expected_id = str(quantity.task_account_daily_coverage_id or "")
    if str(payload.coverage_ledger_id or "") != expected_id:
        return "quarantine", "coverage_binding_conflict"
    if not expected_id:
        return None
    coverage = _coverage(session, quantity, context)
    if coverage is None:
        return "replan_required", "coverage_fact_missing"
    identity = (
        coverage.account_id == action.account_id
        and coverage.task_id == action.task_id
        and coverage.tenant_id == action.tenant_id
        and coverage.task_day_ledger_id == quantity.task_day_ledger_id
    )
    return None if identity else ("quarantine", "coverage_identity_conflict")


def _scope_snapshot(action: Action, payload: SendMessagePayload) -> dict:
    return {
        "content_scope_contract_version": CONTENT_SCOPE_CONTRACT_VERSION,
        "content_scope_tenant_id": action.tenant_id,
        "content_scope_group_id": payload.group_id,
        "content_scope_task_id": str(action.task_id or ""),
    }


def _scope_violation_severity(field: str) -> str:
    if field in {"context_message_ids", "reply_to_message_id", "ai_message_memory_id", "chat_mode"}:
        return "replan_required"
    return "quarantine"


def _gateway_started_open(
    action: Action,
    attempt: ExecutionAttempt | None,
) -> bool:
    return bool(
        action.status in OPEN_STATUSES
        and attempt is not None
        and attempt.gateway_call_started_at is not None
    )


def _latest_attempt(
    session: Session,
    action_id: str,
    context: TakeoverClassificationContext | None,
) -> ExecutionAttempt | None:
    if context:
        return context.latest_attempts.get(action_id)
    return session.scalar(
        select(ExecutionAttempt).where(
            ExecutionAttempt.action_id == action_id,
        ).order_by(ExecutionAttempt.attempt_no.desc()).limit(1)
    )


__all__ = [
    "TakeoverClassification",
    "build_takeover_classification_context",
    "classify_takeover_action",
    "preview_ai_content_scope_takeover",
    "recompute_takeover_hashes",
    "scope_snapshot_payload",
    "takeover_classification_reason_counts",
]
