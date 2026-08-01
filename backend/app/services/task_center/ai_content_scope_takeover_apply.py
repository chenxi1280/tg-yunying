from __future__ import annotations

from collections import Counter
from datetime import datetime
import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    AiContentScopeTakeoverBatch,
    AiContentScopeTakeoverItem,
    AuditLog,
    ContentMixCycleSlot,
    ExecutionAttempt,
)
from app.services._common import _now

from .ai_content_scope_takeover import (
    recompute_takeover_hashes,
    scope_snapshot_payload,
)
from .dispatch_claim_ledger import for_update
from .runtime_state_hash import canonical_state_hash


def begin_takeover_apply(
    session: Session,
    batch_id: str,
    *,
    classification_hash: str,
    expected_counts: dict,
    actor: str,
) -> dict:
    batch = _locked_batch(session, batch_id)
    _validate_apply_identity(
        batch,
        classification_hash=classification_hash,
        expected_counts=expected_counts,
        actor=actor,
    )
    if batch.status == "completed":
        return takeover_batch_summary(session, batch)
    if batch.status == "blocked":
        raise ValueError("takeover_batch_blocked")
    if batch.status == "applying":
        return takeover_batch_summary(session, batch)
    conflicts = _initial_snapshot_conflicts(session, batch)
    if conflicts:
        batch.status = "blocked"
        session.flush()
        _refresh_batch_counts(session, batch)
        return takeover_batch_summary(session, batch)
    batch.status = "applying"
    return takeover_batch_summary(session, batch)


def apply_takeover_chunk(
    session: Session,
    batch_id: str,
    *,
    classification_hash: str,
    actor: str,
    batch_size: int = 100,
) -> dict:
    batch = _locked_batch(session, batch_id)
    if batch.classification_hash != classification_hash:
        raise ValueError("takeover_classification_hash_mismatch")
    if batch.status == "completed":
        return takeover_batch_summary(session, batch)
    if batch.status != "applying":
        raise ValueError("takeover_batch_not_applying")
    items = _locked_pending_items(session, batch.id, batch_size)
    for item in items:
        if not _apply_item(session, batch, item=item, actor=actor):
            break
        batch.last_item_cursor = item.id
    session.flush()
    _refresh_batch_counts(session, batch)
    _finish_batch_if_ready(session, batch)
    return takeover_batch_summary(session, batch)


def takeover_chain_is_complete(
    session: Session,
    head_batch_id: str,
) -> bool:
    chain = _batch_chain(session, head_batch_id)
    if not chain or chain[-1].status != "completed":
        return False
    batch_ids = [batch.id for batch in chain]
    items = list(session.scalars(select(AiContentScopeTakeoverItem).where(
        AiContentScopeTakeoverItem.batch_id.in_(batch_ids),
    )))
    latest = _latest_items_by_action(chain, items)
    if any(item.status not in {"applied", "noop"} for item in latest.values()):
        return False
    applied = Counter(
        item.action_id for item in items if item.status == "applied"
    )
    return all(count <= 1 for count in applied.values())


def takeover_batch_summary(
    session: Session,
    batch: AiContentScopeTakeoverBatch,
) -> dict:
    pending = session.scalar(select(func.count()).select_from(
        AiContentScopeTakeoverItem,
    ).where(
        AiContentScopeTakeoverItem.batch_id == batch.id,
        AiContentScopeTakeoverItem.status == "pending",
    ))
    return {
        "batch_id": batch.id,
        "classification_hash": batch.classification_hash,
        "classification_counts": dict(batch.classification_counts or {}),
        "status": batch.status,
        "pending_count": int(pending or 0),
        "processed_count": batch.processed_count,
        "applied_count": batch.applied_count,
        "noop_count": batch.noop_count,
        "conflict_count": batch.conflict_count,
        "quarantined_count": batch.quarantined_count,
        "last_item_cursor": batch.last_item_cursor,
    }


def _validate_apply_identity(
    batch: AiContentScopeTakeoverBatch,
    *,
    classification_hash: str,
    expected_counts: dict,
    actor: str,
) -> None:
    if batch.classification_hash != classification_hash:
        raise ValueError("takeover_classification_hash_mismatch")
    if dict(batch.classification_counts or {}) != dict(expected_counts or {}):
        raise ValueError("takeover_expected_counts_mismatch")
    if not actor.strip():
        raise ValueError("takeover_actor_required")


def _initial_snapshot_conflicts(
    session: Session,
    batch: AiContentScopeTakeoverBatch,
) -> int:
    items = list(session.scalars(select(AiContentScopeTakeoverItem).where(
        AiContentScopeTakeoverItem.batch_id == batch.id,
        AiContentScopeTakeoverItem.status == "pending",
    ).order_by(AiContentScopeTakeoverItem.action_id.asc())))
    conflict_count = 0
    for item in items:
        action = session.get(Action, item.action_id)
        if action is not None and _item_hashes_match(session, item, action):
            continue
        item.status = "conflict"
        item.processed_at = _now()
        item.outcome = {"reason_code": "preview_state_drift"}
        conflict_count += 1
    return conflict_count


def _apply_item(
    session: Session,
    batch: AiContentScopeTakeoverBatch,
    *,
    item: AiContentScopeTakeoverItem,
    actor: str,
) -> bool:
    action = _locked_action(session, item.action_id)
    if action is None or not _item_hashes_match(session, item, action):
        _mark_item_conflict(item, "apply_state_drift")
        batch.status = "blocked"
        return False
    outcome = _classification_outcome(session, action, item.classification)
    item.status = outcome["item_status"]
    item.outcome = outcome
    item.processed_at = _now()
    _write_item_audit(
        session,
        batch,
        item=item,
        action=action,
        actor=actor,
    )
    if item.status == "quarantined":
        batch.status = "blocked"
        return False
    return True


def _classification_outcome(
    session: Session,
    action: Action,
    classification: str,
) -> dict:
    if classification == "equivalent_snapshot_safe":
        _apply_scope_snapshot(action)
        return {"item_status": "applied", "reason_code": "scope_snapshot_added"}
    if classification == "replan_required":
        _apply_replan(session, action)
        return {"item_status": "applied", "reason_code": "original_obligation_replan"}
    if classification == "remote_reconcile_required":
        case_id = _ensure_remote_case(session, action)
        return {
            "item_status": "noop",
            "reason_code": "remote_reconcile_required",
            "remote_reconcile_case_id": case_id,
        }
    if classification in {"already_current", "immutable_terminal"}:
        return {"item_status": "noop", "reason_code": classification}
    return {"item_status": "quarantined", "reason_code": "classification_quarantine"}


def _apply_scope_snapshot(action: Action) -> None:
    before = _immutable_business_hash(action)
    action.payload = scope_snapshot_payload(action)
    if before != _immutable_business_hash(action):
        raise RuntimeError("takeover_scope_snapshot_changed_business_fields")


def _apply_replan(session: Session, action: Action) -> None:
    from .dispatcher import _finalize_dispatch_action

    action.status = "skipped"
    action.executed_at = _now()
    action.claim_owner = ""
    action.claim_token = ""
    action.claim_expires_at = None
    action.lease_owner = ""
    action.lease_expires_at = None
    action.result = {
        **dict(action.result or {}),
        "success": False,
        "error_code": "content_contract_replan_required",
        "error_message": "历史内容 scope 证据不足，原业务义务等待重规划",
    }
    _finalize_dispatch_action(
        session,
        action,
        project_task_stats=False,
    )
    _release_content_mix_binding(session, action)


def _release_content_mix_binding(session: Session, action: Action) -> None:
    slot_id = str(action.content_mix_cycle_slot_id or "")
    slot = session.get(ContentMixCycleSlot, slot_id) if slot_id else None
    if slot is not None and slot.current_action_id == action.id:
        slot.current_action_id = None


def _ensure_remote_case(session: Session, action: Action) -> str:
    from .remote_reconciliation import ensure_remote_reconcile_case

    attempt = session.scalar(select(ExecutionAttempt).where(
        ExecutionAttempt.action_id == action.id,
        ExecutionAttempt.gateway_call_started_at.is_not(None),
    ).order_by(ExecutionAttempt.attempt_no.desc()).limit(1))
    if attempt is None:
        raise RuntimeError("takeover_remote_attempt_missing")
    return ensure_remote_reconcile_case(session, action, attempt).id


def _item_hashes_match(
    session: Session,
    item: AiContentScopeTakeoverItem,
    action: Action,
) -> bool:
    state_hash, classification = recompute_takeover_hashes(session, action)
    return bool(
        state_hash == item.observed_action_state_hash
        and classification.name == item.classification
        and classification.input_hash == item.classification_input_hash
    )


def _immutable_business_hash(action: Action) -> str:
    payload = dict(action.payload or {})
    for key in (
        "content_scope_contract_version",
        "content_scope_tenant_id",
        "content_scope_group_id",
        "content_scope_task_id",
    ):
        payload.pop(key, None)
    return canonical_state_hash({
        "account_id": action.account_id,
        "scheduled_at": action.scheduled_at,
        "primary_quantity_slot_id": action.primary_quantity_slot_id,
        "content_mix_cycle_slot_id": action.content_mix_cycle_slot_id,
        "payload": payload,
    })


def _mark_item_conflict(
    item: AiContentScopeTakeoverItem,
    reason_code: str,
) -> None:
    item.status = "conflict"
    item.processed_at = _now()
    item.outcome = {"reason_code": reason_code}


def _write_item_audit(
    session: Session,
    batch: AiContentScopeTakeoverBatch,
    *,
    item: AiContentScopeTakeoverItem,
    action: Action,
    actor: str,
) -> None:
    session.add(AuditLog(
        tenant_id=action.tenant_id,
        actor=actor[:100],
        action="AI历史内容scope接管",
        target_type="action",
        target_id=action.id,
        detail=json.dumps({
            "batch_id": batch.id,
            "classification": item.classification,
            "item_status": item.status,
            "reason_code": item.outcome.get("reason_code"),
        }, ensure_ascii=False, sort_keys=True),
    ))


def _refresh_batch_counts(
    session: Session,
    batch: AiContentScopeTakeoverBatch,
) -> None:
    rows = session.execute(select(
        AiContentScopeTakeoverItem.status,
        func.count(AiContentScopeTakeoverItem.id),
    ).where(
        AiContentScopeTakeoverItem.batch_id == batch.id,
    ).group_by(AiContentScopeTakeoverItem.status)).all()
    counts = {status: int(count) for status, count in rows}
    batch.processed_count = sum(
        counts.get(status, 0)
        for status in ("applied", "noop", "conflict", "quarantined")
    )
    batch.applied_count = counts.get("applied", 0)
    batch.noop_count = counts.get("noop", 0)
    batch.conflict_count = counts.get("conflict", 0)
    batch.quarantined_count = counts.get("quarantined", 0)


def _finish_batch_if_ready(
    session: Session,
    batch: AiContentScopeTakeoverBatch,
) -> None:
    pending = session.scalar(select(func.count()).select_from(
        AiContentScopeTakeoverItem,
    ).where(
        AiContentScopeTakeoverItem.batch_id == batch.id,
        AiContentScopeTakeoverItem.status == "pending",
    ))
    if not pending and not batch.conflict_count and not batch.quarantined_count:
        batch.status = "completed"
        batch.completed_at = _now()


def _locked_batch(
    session: Session,
    batch_id: str,
) -> AiContentScopeTakeoverBatch:
    batch = session.scalar(for_update(session, select(
        AiContentScopeTakeoverBatch,
    ).where(AiContentScopeTakeoverBatch.id == batch_id)))
    if batch is None:
        raise ValueError("takeover_batch_not_found")
    return batch


def _locked_pending_items(
    session: Session,
    batch_id: str,
    batch_size: int,
) -> list[AiContentScopeTakeoverItem]:
    statement = select(AiContentScopeTakeoverItem).where(
        AiContentScopeTakeoverItem.batch_id == batch_id,
        AiContentScopeTakeoverItem.status == "pending",
    ).order_by(
        AiContentScopeTakeoverItem.action_id.asc(),
    ).limit(max(1, batch_size))
    return list(session.scalars(for_update(session, statement)))


def _locked_action(session: Session, action_id: str) -> Action | None:
    return session.scalar(for_update(session, select(Action).where(
        Action.id == action_id,
    )))


def _batch_chain(
    session: Session,
    head_batch_id: str,
) -> list[AiContentScopeTakeoverBatch]:
    chain: list[AiContentScopeTakeoverBatch] = []
    seen: set[str] = set()
    current_id: str | None = head_batch_id
    while current_id:
        if current_id in seen:
            raise RuntimeError("takeover_batch_chain_cycle")
        seen.add(current_id)
        batch = session.get(AiContentScopeTakeoverBatch, current_id)
        if batch is None:
            return []
        chain.append(batch)
        current_id = batch.supersedes_batch_id
    return list(reversed(chain))


def _latest_items_by_action(chain, items) -> dict[str, AiContentScopeTakeoverItem]:
    rank = {batch.id: index for index, batch in enumerate(chain)}
    latest: dict[str, AiContentScopeTakeoverItem] = {}
    for item in sorted(items, key=lambda row: rank[row.batch_id]):
        latest[item.action_id] = item
    return latest


__all__ = [
    "apply_takeover_chunk",
    "begin_takeover_apply",
    "takeover_batch_summary",
    "takeover_chain_is_complete",
]
