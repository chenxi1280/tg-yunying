from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Task, TaskStartOperation


@dataclass(frozen=True)
class CreateTaskCommand:
    created_by_user_id: int
    task_type: str
    client_request_id: str
    normalized_request: dict[str, Any]
    start_requested: bool


@dataclass(frozen=True)
class CreateTaskResult:
    task: Task
    create_status: str
    request_fingerprint: str


@dataclass(frozen=True)
class StartTaskCommand:
    start_operation_id: str
    requested_by_user_id: int
    source: str
    replaces_start_operation_id: str | None = None
    replaces_start_operation_version: int | None = None


@dataclass(frozen=True)
class StartExecutionResult:
    task_day_ledger_id: str | None
    runtime_state: str
    runtime_blocker_codes: tuple[str, ...]


@dataclass(frozen=True)
class StartTaskResult:
    task: Task
    start_status: str
    start_failure_code: str
    runtime_state: str
    runtime_blocker_codes: tuple[str, ...]
    start_operation_id: str | None
    start_operation_version: int | None
    start_operation_legacy_untracked: bool


class IdempotencyKeyReused(RuntimeError):
    code = "idempotency_key_reused"

    def __init__(self, task_id: str, conflict_fields: tuple[str, ...]):
        super().__init__(self.code)
        self.task_id = task_id
        self.conflict_fields = conflict_fields


class StartOperationConflict(RuntimeError):
    def __init__(self, code: str, operation: TaskStartOperation | None):
        super().__init__(code)
        self.code = code
        self.operation = operation


TaskBuilder = Callable[[], Task]
TaskStarter = Callable[[Session, Task], StartExecutionResult]


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    if isinstance(value, str):
        return value.strip()
    return value


def _encoded(value: Any) -> bytes:
    return json.dumps(
        _canonical(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _request_envelope(command: CreateTaskCommand) -> dict[str, Any]:
    return {
        **command.normalized_request,
        "task_type": command.task_type,
        "start_requested": command.start_requested,
    }


def _fingerprint(command: CreateTaskCommand) -> tuple[str, dict[str, str]]:
    envelope = _request_envelope(command)
    fingerprint = hashlib.sha256(_encoded(envelope)).hexdigest()
    field_hashes = {
        key: hashlib.sha256(_encoded(value)).hexdigest()
        for key, value in envelope.items()
    }
    return fingerprint, field_hashes


def _existing_task(
    session: Session,
    command: CreateTaskCommand,
) -> Task | None:
    return session.scalar(
        select(Task).where(
            Task.created_by_user_id == command.created_by_user_id,
            Task.create_task_type == command.task_type,
            Task.client_request_id == command.client_request_id,
        )
    )


def _replay_result(
    task: Task,
    fingerprint: str,
    field_hashes: dict[str, str],
) -> CreateTaskResult:
    if task.request_fingerprint == fingerprint:
        return CreateTaskResult(task, "existing_idempotent", fingerprint)
    original = task.request_field_hashes or {}
    conflicts = tuple(
        sorted(
            key
            for key in set(original) | set(field_hashes)
            if original.get(key) != field_hashes.get(key)
        )
    )
    raise IdempotencyKeyReused(task.id, conflicts)


def _prepare_new_task(
    command: CreateTaskCommand,
    builder: TaskBuilder,
    fingerprint: str,
    field_hashes: dict[str, str],
) -> Task:
    task = builder()
    task.created_by_user_id = command.created_by_user_id
    task.create_task_type = command.task_type
    task.client_request_id = command.client_request_id
    task.request_fingerprint = fingerprint
    task.request_field_hashes = field_hashes
    task.idempotency_legacy_unproven = False
    return task


def create_task_once(
    session: Session,
    command: CreateTaskCommand,
    builder: TaskBuilder,
) -> CreateTaskResult:
    fingerprint, field_hashes = _fingerprint(command)
    existing = _existing_task(session, command)
    if existing is not None:
        return _replay_result(existing, fingerprint, field_hashes)
    task = _prepare_new_task(command, builder, fingerprint, field_hashes)
    session.add(task)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        concurrent = _existing_task(session, command)
        if concurrent is None:
            raise
        return _replay_result(concurrent, fingerprint, field_hashes)
    session.refresh(task)
    return CreateTaskResult(task, "created", fingerprint)


def _locked_task(session: Session, task_id: str) -> Task:
    task = session.scalar(
        select(Task).where(Task.id == task_id).with_for_update()
    )
    if task is None:
        raise ValueError("task_not_found")
    return task


def _current_operation(
    session: Session,
    task_id: str,
    *,
    lock: bool,
) -> TaskStartOperation | None:
    statement = select(TaskStartOperation).where(
        TaskStartOperation.task_id == task_id
    )
    if lock:
        statement = statement.with_for_update()
    return session.scalar(statement)


def _runtime_projection(task: Task) -> tuple[str, tuple[str, ...]]:
    stats = task.stats or {}
    state = str(stats.get("runtime_state") or "runnable")
    blockers = tuple(str(item) for item in stats.get("runtime_blocker_codes") or ())
    return state, blockers


def _started_result(
    task: Task,
    operation: TaskStartOperation | None,
) -> StartTaskResult:
    runtime_state, blockers = _runtime_projection(task)
    return StartTaskResult(
        task=task,
        start_status="started",
        start_failure_code="",
        runtime_state=runtime_state,
        runtime_blocker_codes=blockers,
        start_operation_id=operation.start_operation_id if operation else None,
        start_operation_version=operation.operation_version if operation else None,
        start_operation_legacy_untracked=operation is None,
    )


def _check_replace(
    command: StartTaskCommand,
    current: TaskStartOperation,
) -> None:
    if current.start_operation_id == command.start_operation_id:
        return
    expected = (
        command.replaces_start_operation_id,
        command.replaces_start_operation_version,
    )
    actual = (current.start_operation_id, current.operation_version)
    if expected != actual:
        raise StartOperationConflict("stale_start_operation", current)


def _begin_operation(
    session: Session,
    task: Task,
    command: StartTaskCommand,
) -> tuple[TaskStartOperation, str | None, int]:
    current = _current_operation(session, task.id, lock=True)
    if current is not None and current.status == "processing":
        raise StartOperationConflict("start_in_progress", current)
    if current is not None:
        _check_replace(command, current)
    previous_id = current.start_operation_id if current else None
    previous_version = current.operation_version if current else 0
    operation = current or TaskStartOperation(task_id=task.id)
    operation.start_operation_id = command.start_operation_id
    operation.operation_version = previous_version + 1
    operation.requested_by_user_id = command.requested_by_user_id
    operation.source = command.source
    operation.status = "processing"
    operation.task_day_ledger_id = None
    operation.start_failure_code = ""
    session.add(operation)
    session.flush()
    return operation, previous_id, previous_version


def _store_runtime(task: Task, result: StartExecutionResult) -> None:
    stats = dict(task.stats or {})
    stats["runtime_state"] = result.runtime_state
    stats["runtime_blocker_codes"] = list(result.runtime_blocker_codes)
    task.stats = stats


def _failure_code(error: Exception) -> str:
    return (str(error).strip() or error.__class__.__name__)[:80]


def _write_start_failure(
    session: Session,
    task_id: str,
    command: StartTaskCommand,
    previous_id: str | None,
    previous_version: int,
    error: Exception,
) -> StartTaskResult:
    task = _locked_task(session, task_id)
    current = _current_operation(session, task_id, lock=True)
    current_tuple = (
        current.start_operation_id if current else None,
        current.operation_version if current else 0,
    )
    if task.status not in {"draft", "stopped"}:
        return _started_result(task, current)
    if current_tuple != (previous_id, previous_version):
        return _failure_result(task, current)
    operation = current or TaskStartOperation(task_id=task_id)
    operation.start_operation_id = command.start_operation_id
    operation.operation_version = previous_version + 1
    operation.requested_by_user_id = command.requested_by_user_id
    operation.source = command.source
    operation.status = "failed"
    operation.task_day_ledger_id = None
    operation.start_failure_code = _failure_code(error)
    session.add(operation)
    session.commit()
    session.refresh(task)
    return _failure_result(task, operation)


def _failure_result(
    task: Task,
    operation: TaskStartOperation | None,
) -> StartTaskResult:
    return StartTaskResult(
        task=task,
        start_status="start_failed",
        start_failure_code=operation.start_failure_code if operation else "",
        runtime_state="not_started",
        runtime_blocker_codes=(),
        start_operation_id=operation.start_operation_id if operation else None,
        start_operation_version=operation.operation_version if operation else None,
        start_operation_legacy_untracked=operation is None,
    )


def start_task_once(
    session: Session,
    task_id: str,
    command: StartTaskCommand,
    starter: TaskStarter,
) -> StartTaskResult:
    task = _locked_task(session, task_id)
    current = _current_operation(session, task_id, lock=True)
    if task.status in {"running", "paused"}:
        return _started_result(task, current)
    operation, previous_id, previous_version = _begin_operation(
        session,
        task,
        command,
    )
    try:
        execution = starter(session, task)
        operation.status = "started"
        operation.task_day_ledger_id = execution.task_day_ledger_id
        _store_runtime(task, execution)
        session.commit()
        session.refresh(task)
        return _started_result(task, operation)
    except Exception as error:
        session.rollback()
        return _write_start_failure(
            session,
            task_id,
            command,
            previous_id,
            previous_version,
            error,
        )


__all__ = [
    "CreateTaskCommand",
    "CreateTaskResult",
    "IdempotencyKeyReused",
    "StartExecutionResult",
    "StartOperationConflict",
    "StartTaskCommand",
    "StartTaskResult",
    "create_task_once",
    "start_task_once",
]
