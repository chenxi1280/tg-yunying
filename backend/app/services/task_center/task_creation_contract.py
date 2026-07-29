from __future__ import annotations

import hashlib
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy import select

from app.models import Task, TaskStartOperation
from app.schemas.task_center import TaskOut
from app.services._common import audit

from .creation_operations import (
    CreateTaskCommand,
    CreateTaskResult,
    StartTaskCommand,
    StartTaskResult,
    create_task_once,
    start_task_once,
)


@dataclass(frozen=True)
class TaskCreationContractResult:
    create: CreateTaskResult
    start: StartTaskResult | None

    @property
    def task(self) -> Task:
        return self.create.task


def _normalized_request(payload: Any) -> dict[str, Any]:
    return payload.model_dump(
        mode="json",
        exclude={"client_request_id"},
        exclude_none=True,
    )


def _start_operation_id(client_request_id: str) -> str:
    digest = hashlib.sha256(
        f"create-and-start:{client_request_id}".encode("utf-8")
    ).hexdigest()
    return f"cas-{digest[:48]}"


def _task_builder(
    session: Session,
    tenant_id: int,
    task_type: str,
    payload: Any,
    actor: str,
):
    from . import service

    def build() -> Task:
        effective_payload = payload
        if task_type == "search_click":
            effective_payload = service._search_click_internal_payload(
                session, tenant_id, payload
            )
        if task_type == "search_rank_deboost":
            effective_payload = service._simple_search_rank_deboost_payload(
                session, tenant_id, payload
            )
            return service.create_search_rank_deboost_task(
                session,
                tenant_id,
                effective_payload,
                actor,
                defer_readiness=True,
                commit=False,
            )
        task = service._new_task(session, tenant_id, task_type, effective_payload)
        audit(
            session,
            tenant_id=tenant_id,
            actor=actor,
            action="创建任务中心任务",
            target_type="task",
            target_id=task.id,
            detail=task_type,
        )
        return task

    return build


def execute_task_creation_contract(
    session: Session,
    *,
    tenant_id: int,
    user_id: int,
    actor: str,
    task_type: str,
    payload: Any,
    start_requested: bool,
) -> TaskCreationContractResult:
    client_request_id = str(payload.client_request_id or "").strip()
    if not client_request_id:
        raise ValueError("client_request_id_required")
    command = CreateTaskCommand(
        created_by_user_id=user_id,
        task_type=task_type,
        client_request_id=client_request_id,
        normalized_request=_normalized_request(payload),
        start_requested=start_requested,
    )
    created = create_task_once(
        session,
        command,
        _task_builder(session, tenant_id, task_type, payload, actor),
    )
    if not start_requested:
        return TaskCreationContractResult(created, None)
    start_command = StartTaskCommand(
        start_operation_id=_start_operation_id(client_request_id),
        requested_by_user_id=user_id,
        source="create_and_start",
    )
    from .service import start_task_in_transaction

    started = start_task_once(
        session,
        created.task.id,
        start_command,
        lambda current_session, task: start_task_in_transaction(
            current_session, task, actor
        ),
    )
    return TaskCreationContractResult(created, started)


def task_creation_response(result: TaskCreationContractResult) -> dict[str, Any]:
    task = result.task
    stats = task.stats or {}
    start = result.start
    return TaskOut.model_validate(task).model_dump() | {
        "create_status": result.create.create_status,
        "start_status": start.start_status if start else "not_requested",
        "start_failure_code": start.start_failure_code if start else "",
        "runtime_state": (
            start.runtime_state
            if start
            else str(stats.get("runtime_state") or "runnable")
        ),
        "runtime_blocker_codes": (
            list(start.runtime_blocker_codes)
            if start
            else list(stats.get("runtime_blocker_codes") or ())
        ),
        "start_operation_id": start.start_operation_id if start else None,
        "start_operation_version": start.start_operation_version if start else None,
        "start_operation_legacy_untracked": (
            start.start_operation_legacy_untracked if start else False
        ),
    }


def execute_task_start_contract(
    session: Session,
    *,
    tenant_id: int,
    user_id: int,
    actor: str,
    task_id: str,
    payload: Any,
) -> StartTaskResult:
    task_exists = session.scalar(
        select(Task.id).where(Task.id == task_id, Task.tenant_id == tenant_id)
    )
    if task_exists is None:
        raise ValueError("task not found")
    payload = payload or _legacy_start_request(session, task_id)
    command = StartTaskCommand(
        start_operation_id=payload.start_operation_id,
        requested_by_user_id=user_id,
        source="explicit_start",
        replaces_start_operation_id=payload.replaces_start_operation_id,
        replaces_start_operation_version=payload.replaces_start_operation_version,
    )
    from .service import start_task_in_transaction

    return start_task_once(
        session,
        task_id,
        command,
        lambda current_session, task: start_task_in_transaction(
            current_session, task, actor
        ),
    )


def _legacy_start_request(session: Session, task_id: str):
    current = session.get(TaskStartOperation, task_id)
    version = current.operation_version if current else 0
    return SimpleNamespace(
        start_operation_id=f"legacy-start:{task_id}:{version + 1}",
        replaces_start_operation_id=(
            current.start_operation_id if current else None
        ),
        replaces_start_operation_version=version or None,
    )


def task_start_response(result: StartTaskResult) -> dict[str, Any]:
    return TaskOut.model_validate(result.task).model_dump() | {
        "create_status": "existing",
        "start_status": result.start_status,
        "start_failure_code": result.start_failure_code,
        "runtime_state": result.runtime_state,
        "runtime_blocker_codes": list(result.runtime_blocker_codes),
        "start_operation_id": result.start_operation_id,
        "start_operation_version": result.start_operation_version,
        "start_operation_legacy_untracked": result.start_operation_legacy_untracked,
    }


__all__ = [
    "TaskCreationContractResult",
    "execute_task_creation_contract",
    "execute_task_start_contract",
    "task_creation_response",
    "task_start_response",
]
