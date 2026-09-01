from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ExecutionAttempt,
    FulfillmentRemoteFact,
    GatewayRequestEvidenceJournal,
    RemoteMutationTombstone,
    Task,
    TaskContractActivationManifest,
    TaskContractRoute,
    TaskDeleteOperation,
    TaskDeleteOperationItem,
)
from app.services._common import _now

from .channel_comment_delete_tombstone import (
    COMMENT_OUTCOME_ITEM,
    comment_outcome_snapshots,
    comment_tombstone_values,
)
from .physical_task_cleanup import delete_task_runtime_rows


DELETE_BATCH_SIZE = 1000
INFLIGHT_ACTION_STATES = frozenset({"claiming", "executing"})
REMOTE_TOMBSTONE_ACTION_STATES = frozenset({"success", "unknown_after_send"})


@dataclass(frozen=True)
class DeleteRequest:
    task_id: str
    manifest_id: str
    expected_manifest_hash: str
    actor: str
    approval_ref: str


def prepare_task_deletion(session: Session, request: DeleteRequest) -> TaskDeleteOperation:
    manifest, route = _active_manifest_route(session, request)
    task = session.get(Task, request.task_id)
    if task is None:
        raise ValueError("physical_delete_task_not_found")
    expected_epoch = int(route.expected_lifecycle_epoch)
    if int(task.task_lifecycle_epoch or 1) != expected_epoch:
        raise ValueError("physical_delete_lifecycle_epoch_conflict")
    operation = TaskDeleteOperation(
        original_task_id=task.id,
        expected_lifecycle_epoch=expected_epoch,
        expected_manifest_hash=manifest.old_set_hash,
        state="fencing",
        resume_stage="snapshot_runtime",
        created_by=request.actor,
        approval_ref=request.approval_ref,
    )
    session.add(operation)
    session.flush()
    changed = session.execute(
        update(Task)
        .where(Task.id == task.id, Task.task_lifecycle_epoch == expected_epoch)
        .values(status="deleting", next_run_at=None, task_lifecycle_epoch=expected_epoch + 1)
    ).rowcount
    if changed != 1:
        raise ValueError("physical_delete_lifecycle_epoch_conflict")
    session.refresh(task)
    if task.type == "channel_comment":
        from .channel_comment_lifecycle import delete_channel_comment_plans

        delete_channel_comment_plans(session, task, occurred_at=_now())
    return operation


def advance_task_deletion(
    session: Session,
    operation_id: str,
    *,
    expected_stage_version: int,
) -> TaskDeleteOperation:
    operation = session.get(TaskDeleteOperation, operation_id)
    if operation is None:
        raise ValueError("physical_delete_operation_not_found")
    if int(operation.stage_version or 1) != expected_stage_version:
        raise ValueError("physical_delete_stage_version_conflict")
    handlers = {
        "fencing": _snapshot_runtime,
        "snapshot_committed": _write_tombstones,
        "tombstones_written": _verify_tombstones,
        "tombstone_verified": _begin_delete,
        "deleting": _delete_runtime,
    }
    handler = handlers.get(operation.state)
    if handler is None:
        if operation.state == "committed":
            return operation
        raise ValueError("physical_delete_operation_state_invalid")
    values = handler(session, operation)
    next_state = str(values.pop("state"))
    changed = session.execute(
        update(TaskDeleteOperation)
        .where(
            TaskDeleteOperation.id == operation.id,
            TaskDeleteOperation.state == operation.state,
            TaskDeleteOperation.stage_version == expected_stage_version,
        )
        .values(**values, state=next_state, stage_version=expected_stage_version + 1)
    ).rowcount
    if changed != 1:
        raise ValueError("physical_delete_stage_version_conflict")
    session.flush()
    session.refresh(operation)
    return operation


def _active_manifest_route(
    session: Session,
    request: DeleteRequest,
) -> tuple[TaskContractActivationManifest, TaskContractRoute]:
    manifest = session.get(TaskContractActivationManifest, request.manifest_id)
    if manifest is None or manifest.state != "active":
        raise ValueError("physical_delete_manifest_not_active")
    if request.expected_manifest_hash != manifest.old_set_hash:
        raise ValueError("physical_delete_manifest_hash_mismatch")
    if not request.approval_ref.strip():
        raise ValueError("physical_delete_approval_ref_required")
    route = session.scalar(select(TaskContractRoute).where(
        TaskContractRoute.manifest_id == manifest.id,
        TaskContractRoute.task_id == request.task_id,
        TaskContractRoute.role == "old",
    ))
    if route is None:
        raise ValueError("physical_delete_task_not_in_old_set")
    return manifest, route


def _snapshot_runtime(session: Session, operation: TaskDeleteOperation) -> dict:
    task = _fenced_task(session, operation)
    _record_item(
        session,
        operation.id,
        entity_type="task",
        entity_id=task.id,
        state_hash=_task_hash(task),
    )
    if session.scalar(select(Action.id).where(
        Action.task_id == task.id,
        Action.status.in_(INFLIGHT_ACTION_STATES),
    ).limit(1)) is not None:
        raise ValueError("physical_delete_inflight_actions_remaining")
    action_count = int(session.scalar(select(func.count(Action.id)).where(
        Action.task_id == task.id,
    )) or 0)
    remote_actions = _remote_candidate_actions(session, task.id)
    _record_remote_items(session, operation.id, remote_actions)
    comment_outcomes = comment_outcome_snapshots(session, task.id)
    for outcome in comment_outcomes:
        _record_item(
            session, operation.id, entity_type=COMMENT_OUTCOME_ITEM,
            entity_id=outcome.plan_id, state_hash=outcome.state_hash,
        )
    item_hash = _item_set_hash(session, operation.id)
    return {
        "state": "snapshot_committed",
        "resume_stage": "write_tombstones",
        "delete_set_hash": item_hash,
        "counts": {
            "tasks": 1,
            "actions": action_count,
            "remote_candidates": len(remote_actions),
            "channel_comment_outcomes": len(comment_outcomes),
        },
        "checkpoint": {"snapshot_hash": item_hash},
    }


def _write_tombstones(session: Session, operation: TaskDeleteOperation) -> dict:
    _require_snapshot_unchanged(session, operation)
    action_ids = _item_ids(session, operation.id, "remote_action")
    values = _tombstone_values_for_actions(
        session,
        operation.original_task_id,
        action_ids,
    )
    values.extend(comment_tombstone_values(
        session, _fenced_task(session, operation), operation.id,
    ))
    _insert_tombstones(session, values)
    pairs = {_pair(item) for item in values}
    tombstone_hash = _hash("|".join(sorted(pairs)))
    counts = dict(operation.counts or {})
    counts["remote_tombstones"] = len(pairs)
    return {
        "state": "tombstones_written",
        "resume_stage": "verify_tombstones",
        "tombstone_set_hash": tombstone_hash,
        "counts": counts,
    }


def _verify_tombstones(session: Session, operation: TaskDeleteOperation) -> dict:
    pairs = _expected_tombstone_pairs(session, operation)
    _require_tombstone_pairs_exist(session, pairs)
    expected_count = int(dict(operation.counts or {}).get("remote_tombstones") or 0)
    if len(pairs) != expected_count or _hash("|".join(sorted(pairs))) != operation.tombstone_set_hash:
        raise ValueError("physical_delete_tombstone_set_changed")
    return {"state": "tombstone_verified", "resume_stage": "begin_delete"}


def _begin_delete(session: Session, operation: TaskDeleteOperation) -> dict:
    _require_snapshot_unchanged(session, operation)
    return {"state": "deleting", "resume_stage": "delete_runtime"}


def _delete_runtime(session: Session, operation: TaskDeleteOperation) -> dict:
    task = _fenced_task(session, operation)
    _require_tombstone_pairs_exist(session, _expected_tombstone_pairs(session, operation))
    delete_task_runtime_rows(session, task.id)
    changed = session.execute(delete(Task).where(
        Task.id == task.id,
        Task.status == "deleting",
        Task.task_lifecycle_epoch == int(operation.expected_lifecycle_epoch) + 1,
    )).rowcount
    if changed != 1:
        raise RuntimeError("physical_delete_task_cas_failed")
    session.execute(
        update(TaskDeleteOperationItem)
        .where(TaskDeleteOperationItem.operation_id == operation.id)
        .values(state="deleted", deleted_at=_now())
    )
    return {
        "state": "committed",
        "resume_stage": "complete",
        "committed_at": _now(),
    }


def _require_snapshot_unchanged(session: Session, operation: TaskDeleteOperation) -> None:
    task = _fenced_task(session, operation)
    if _current_runtime_hash(session, task) != operation.delete_set_hash:
        raise ValueError("physical_delete_snapshot_changed")


def _current_runtime_hash(session: Session, task: Task) -> str:
    rows = [("task", task.id, _task_hash(task))]
    for action in _remote_candidate_actions(session, task.id):
        rows.append(("remote_action", action.id, _action_hash(action)))
    for outcome in comment_outcome_snapshots(session, task.id):
        rows.append((COMMENT_OUTCOME_ITEM, outcome.plan_id, outcome.state_hash))
    return _hash("|".join(":".join(row) for row in sorted(rows)))


def _fenced_task(session: Session, operation: TaskDeleteOperation) -> Task:
    task = session.get(Task, operation.original_task_id)
    if (
        task is None
        or task.status != "deleting"
        or int(task.task_lifecycle_epoch or 1) != int(operation.expected_lifecycle_epoch) + 1
    ):
        raise ValueError("physical_delete_task_fence_lost")
    return task


def _record_item(
    session: Session,
    operation_id: str,
    *,
    entity_type: str,
    entity_id: str,
    state_hash: str,
) -> None:
    statement = _insert(session, TaskDeleteOperationItem.__table__)
    session.execute(statement.values(
        operation_id=operation_id,
        entity_type=entity_type,
        entity_id=entity_id,
        expected_state_hash=state_hash,
        state="frozen",
    ).on_conflict_do_nothing(index_elements=["operation_id", "entity_type", "entity_id"]))


def _record_remote_items(
    session: Session,
    operation_id: str,
    actions: list[Action],
) -> None:
    for batch in _batches(actions):
        values = [{
            "operation_id": operation_id,
            "entity_type": "remote_action",
            "entity_id": action.id,
            "expected_state_hash": _action_hash(action),
            "state": "frozen",
        } for action in batch]
        statement = _insert(session, TaskDeleteOperationItem.__table__)
        session.execute(statement.values(values).on_conflict_do_nothing(
            index_elements=["operation_id", "entity_type", "entity_id"],
        ))


def _item_ids(session: Session, operation_id: str, entity_type: str) -> list[str]:
    return list(session.scalars(select(TaskDeleteOperationItem.entity_id).where(
        TaskDeleteOperationItem.operation_id == operation_id,
        TaskDeleteOperationItem.entity_type == entity_type,
    ).order_by(TaskDeleteOperationItem.entity_id)))


def _item_set_hash(session: Session, operation_id: str) -> str:
    rows = session.execute(select(
        TaskDeleteOperationItem.entity_type,
        TaskDeleteOperationItem.entity_id,
        TaskDeleteOperationItem.expected_state_hash,
    ).where(TaskDeleteOperationItem.operation_id == operation_id).order_by(
        TaskDeleteOperationItem.entity_type,
        TaskDeleteOperationItem.entity_id,
    )).all()
    return _hash("|".join(":".join(row) for row in rows))


def _tombstone_values(
    task_id: str,
    action: Action,
    attempt: ExecutionAttempt | None,
    fact: FulfillmentRemoteFact | None,
    journal: GatewayRequestEvidenceJournal | None,
) -> dict:
    mutation_hash = fact.remote_mutation_key_hash if fact else _hash(str(action.action_dedupe_key or action.id))
    request_hash = fact.gateway_request_hash if fact else _hash(
        str(journal.gateway_request_identity if journal else action.id)
    )
    mutation_state = str(journal.remote_mutation_state or "unknown") if journal else "unknown"
    return {
        "tenant_id": action.tenant_id,
        "original_task_id": task_id,
        "mutation_kind": action.action_type,
        "remote_mutation_key_hash": mutation_hash,
        "gateway_request_hash": request_hash,
        "remote_started": mutation_state != "false",
        "terminal_state": action.status,
        "remote_fact_identity_hash": fact.fact_identity_hash if fact else "",
        "reconcile_state": "open" if action.status in {"executing", "unknown_after_send"} else "closed",
        "observed_at": _now(),
    }


def _expected_tombstone_pairs(
    session: Session,
    operation: TaskDeleteOperation,
) -> list[str]:
    values = _tombstone_values_for_actions(
        session,
        operation.original_task_id,
        _item_ids(session, operation.id, "remote_action"),
    )
    values.extend(comment_tombstone_values(
        session, _fenced_task(session, operation), operation.id,
    ))
    pairs = {_pair(item) for item in values}
    return sorted(pairs)


def _require_tombstone_pairs_exist(session: Session, pairs: list[str]) -> None:
    for pair in pairs:
        mutation_hash, request_hash = pair.split(":", 1)
        exists = session.scalar(select(RemoteMutationTombstone.id).where(
            RemoteMutationTombstone.remote_mutation_key_hash == mutation_hash,
            RemoteMutationTombstone.gateway_request_hash == request_hash,
        ))
        if exists is None:
            raise ValueError("physical_delete_tombstone_missing")


def _remote_candidate_actions(session: Session, task_id: str) -> list[Action]:
    started_attempts = select(ExecutionAttempt.action_id).where(
        ExecutionAttempt.gateway_call_started_at.is_not(None),
    )
    return list(session.scalars(select(Action).where(
        Action.task_id == task_id,
        or_(
            Action.status.in_(REMOTE_TOMBSTONE_ACTION_STATES),
            Action.id.in_(started_attempts),
        ),
    ).order_by(Action.id)))


def _tombstone_values_for_actions(
    session: Session,
    task_id: str,
    action_ids: list[str],
) -> list[dict]:
    values: list[dict] = []
    for batch in _batches(action_ids):
        actions = list(session.scalars(select(Action).where(Action.id.in_(batch))))
        if len(actions) != len(batch):
            raise ValueError("physical_delete_frozen_action_missing")
        attempts = _latest_attempts(session, batch)
        facts = _latest_facts(session, batch)
        journals = _journals_by_attempt(session, attempts.values())
        values.extend(_tombstone_values(
            task_id,
            action,
            attempts.get(action.id),
            facts.get(action.id),
            journals.get(attempts[action.id].id) if action.id in attempts else None,
        ) for action in actions)
    return values


def _latest_attempts(
    session: Session,
    action_ids: list[str],
) -> dict[str, ExecutionAttempt]:
    rows = session.scalars(select(ExecutionAttempt).where(
        ExecutionAttempt.action_id.in_(action_ids),
    ).order_by(ExecutionAttempt.action_id, ExecutionAttempt.attempt_no.desc()))
    latest: dict[str, ExecutionAttempt] = {}
    for attempt in rows:
        latest.setdefault(attempt.action_id, attempt)
    return latest


def _latest_facts(
    session: Session,
    action_ids: list[str],
) -> dict[str, FulfillmentRemoteFact]:
    rows = session.scalars(select(FulfillmentRemoteFact).where(
        FulfillmentRemoteFact.action_id.in_(action_ids),
    ).order_by(FulfillmentRemoteFact.action_id, FulfillmentRemoteFact.observed_at.desc()))
    latest: dict[str, FulfillmentRemoteFact] = {}
    for fact in rows:
        latest.setdefault(fact.action_id, fact)
    return latest


def _journals_by_attempt(
    session: Session,
    attempts: Iterable[ExecutionAttempt],
) -> dict[str, GatewayRequestEvidenceJournal]:
    attempt_ids = [attempt.id for attempt in attempts]
    if not attempt_ids:
        return {}
    rows = session.scalars(select(GatewayRequestEvidenceJournal).where(
        GatewayRequestEvidenceJournal.execution_attempt_id.in_(attempt_ids),
    ).order_by(GatewayRequestEvidenceJournal.observed_at.desc()))
    journals: dict[str, GatewayRequestEvidenceJournal] = {}
    for journal in rows:
        journals.setdefault(journal.execution_attempt_id, journal)
    return journals


def _insert_tombstones(session: Session, values: list[dict]) -> None:
    for batch in _batches(values):
        statement = _insert(session, RemoteMutationTombstone.__table__)
        session.execute(statement.values(batch).on_conflict_do_nothing(
            index_elements=["remote_mutation_key_hash", "gateway_request_hash"],
        ))


def _batches(values: list) -> list[list]:
    return [
        values[offset:offset + DELETE_BATCH_SIZE]
        for offset in range(0, len(values), DELETE_BATCH_SIZE)
    ]


def _task_hash(task: Task) -> str:
    return _hash(f"{task.id}:{task.status}:{int(task.task_lifecycle_epoch or 1)}")


def _action_hash(action: Action) -> str:
    return _hash(f"{action.id}:{action.status}:{int(action.action_version or 1)}")


def _pair(values: dict) -> str:
    return f"{values['remote_mutation_key_hash']}:{values['gateway_request_hash']}"


def _insert(session: Session, table):
    return pg_insert(table) if session.get_bind().dialect.name == "postgresql" else sqlite_insert(table)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


__all__ = ["DeleteRequest", "advance_task_deletion", "prepare_task_deletion"]
