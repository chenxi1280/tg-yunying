from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.auth import CurrentUser, ensure_permission, get_current_user
from app.database import get_session as get_db
from app.models.enums import now
from app.models.group_clone import (
    CloneDeliveryObligation,
    CloneManualReviewDecision,
    CloneMessagePart,
    CloneSenderBindingHistory,
    CloneSequencerHeadCase,
    CloneSourceEvent,
    CloneSourceStreamState,
)
from app.models.task_center import Task
from app.models.telegram_authorities import (
    TelegramGroupMutationAuthority,
    TelegramGroupMutationAuthorityHolder,
)
from app.models.telegram_updates import TelegramAuthorizationUpdateState
from app.schemas.task_center import (
    GroupClonePrecheckResponse,
    GroupCloneSequencerHeadDecisionRequest,
    GroupCloneTaskConfigUpdate,
    GroupCloneTaskCreate,
)
from app.services.task_center.group_mutation_authority import (
    check_and_claim_exclusive_authority,
    compute_route_hash,
    release_exclusive_authority,
)
from app.services.task_center.group_clone_lifecycle import (
    create_and_start_group_clone_task as create_and_start_clone,
    create_group_clone_task as create_clone,
    precheck_group_clone as run_clone_precheck,
    tenant_clone_task,
    update_group_clone_config,
)
from app.services.task_center.group_clone_read_model import (
    manual_review_items,
    message_mapping_items,
    reconcile_case_items,
    update_ingress_status,
)
from app.services.task_center.group_clone_sequencer_decision import (
    decide_clone_sequencer_case,
)

router = APIRouter(prefix="/api/tasks", tags=["group_clone"])


@router.post("/group-clone/precheck", response_model=GroupClonePrecheckResponse)
def precheck_group_clone(
    req: GroupCloneTaskCreate,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> GroupClonePrecheckResponse:
    ensure_permission(current_user, "tasks.manage")
    try:
        result = run_clone_precheck(db, current_user.tenant_id or 1, req)
        db.commit()
        return result
    except (ValueError, RuntimeError):
        db.rollback()
        raise


@router.post("/group-clone", status_code=status.HTTP_201_CREATED)
def create_group_clone_task(
    req: GroupCloneTaskCreate,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    ensure_permission(current_user, "tasks.manage")
    try:
        task, created = create_clone(db, current_user.tenant_id or 1, current_user.id, payload=req)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return {"success": True, "created": created, "task_id": task.id, "status": task.status}


@router.post("/group-clone/create-and-start", status_code=status.HTTP_201_CREATED)
def create_and_start_group_clone_task(
    req: GroupCloneTaskCreate,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    ensure_permission(current_user, "tasks.manage")
    try:
        task, created = create_and_start_clone(db, current_user.tenant_id or 1, current_user.id, payload=req)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    start_state = (task.stats or {}).get("clone_start_state", "")
    return {"success": True, "created": created, "task_id": task.id, "status": task.status, "clone_start_state": start_state}


@router.patch("/{task_id}/group-clone")
def update_group_clone_task(
    task_id: str,
    *,
    req: GroupCloneTaskConfigUpdate,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    """
    修改 group_clone 任务配置。
    """
    ensure_permission(current_user, "tasks.manage")
    try:
        task = update_group_clone_config(db, current_user.tenant_id or 1, task_id, payload=req)
        db.commit()
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"success": True, "task_id": task.id, "config_revision": task.config_revision}


@router.get("/{task_id}/clone-source-events")
def list_clone_source_events(
    task_id: str,
    *,
    limit: int = Query(default=50, ge=1, le=200),
    before_stream_order_no: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    """
    获取源群捕获的事件流水列表。
    """
    ensure_permission(current_user, "tasks.view")
    _require_clone_task(db, current_user, task_id)
    stmt = select(CloneSourceEvent).where(CloneSourceEvent.task_id == task_id)
    if before_stream_order_no is not None:
        stmt = stmt.where(CloneSourceEvent.stream_order_no < before_stream_order_no)
    stmt = stmt.order_by(CloneSourceEvent.stream_order_no.desc()).limit(limit)
    items = db.execute(stmt).scalars().all()
    total = db.scalar(select(func.count()).select_from(CloneSourceEvent).where(CloneSourceEvent.task_id == task_id)) or 0
    return {
        "total": total,
        "next_cursor": items[-1].stream_order_no if len(items) == limit else None,
        "items": [
            {
                "id": ev.id,
                "stream_order_no": ev.stream_order_no,
                "event_type": ev.event_type,
                "source_message_id": ev.source_message_id,
                "sender_peer_id": ev.sender_peer_id,
                "content": ev.content[:100] if ev.content else "",
                "observed_at": ev.observed_at.isoformat() if ev.observed_at else None,
            }
            for ev in items
        ],
    }


@router.get("/{task_id}/clone-obligations")
def list_clone_obligations(
    task_id: str,
    *,
    state: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
    before_stream_order_no: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    """
    获取送达义务状态列表。
    """
    ensure_permission(current_user, "tasks.view")
    _require_clone_task(db, current_user, task_id)
    query = select(CloneDeliveryObligation).where(CloneDeliveryObligation.task_id == task_id)
    if state:
        query = query.where(CloneDeliveryObligation.state == state)
    if before_stream_order_no is not None:
        query = query.where(
            CloneDeliveryObligation.stream_order_no < before_stream_order_no,
        )
    query = query.order_by(CloneDeliveryObligation.stream_order_no.desc()).limit(limit)

    items = db.execute(query).scalars().all()
    return {
        "next_cursor": items[-1].stream_order_no if len(items) == limit else None,
        "items": [
            {
                "id": o.id,
                "stream_order_no": o.stream_order_no,
                "obligation_kind": o.obligation_kind,
                "state": o.state,
                "planned_at": o.planned_at.isoformat() if o.planned_at else None,
                "sequencer_head_case_id": o.sequencer_head_case_id,
            }
            for o in items
        ]
    }


@router.get("/{task_id}/clone-bindings")
def list_clone_bindings(
    task_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    before_last_spoken_at: datetime | None = None,
    before_id: str | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    """
    获取当前发言人与受控账号绑定关系。
    """
    ensure_permission(current_user, "tasks.view")
    _require_clone_task(db, current_user, task_id)
    stmt = select(CloneSenderBindingHistory).where(
        CloneSenderBindingHistory.task_id == task_id,
    )
    if before_last_spoken_at is not None:
        cursor_clause = CloneSenderBindingHistory.last_spoken_at < before_last_spoken_at
        if before_id:
            cursor_clause = or_(
                cursor_clause,
                and_(
                    CloneSenderBindingHistory.last_spoken_at == before_last_spoken_at,
                    CloneSenderBindingHistory.id < before_id,
                ),
            )
        stmt = stmt.where(cursor_clause)
    stmt = stmt.order_by(
        CloneSenderBindingHistory.last_spoken_at.desc(),
        CloneSenderBindingHistory.id.desc(),
    ).limit(limit)
    items = db.execute(stmt).scalars().all()
    return {
        "next_cursor": ({
            "last_spoken_at": items[-1].last_spoken_at.isoformat(),
            "id": items[-1].id,
        } if len(items) == limit else None),
        "items": [
            {
                "id": b.id,
                "source_sender_peer_id": b.source_sender_peer_id,
                "source_sender_name": b.source_sender_name,
                "assigned_account_id": b.assigned_account_id,
                "status": b.status,
                "is_vip": b.is_vip,
                "last_spoken_at": b.last_spoken_at.isoformat() if b.last_spoken_at else None,
            }
            for b in items
        ]
    }


@router.get("/{task_id}/clone-message-mappings")
def list_clone_message_mappings(
    task_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    before_source_message_id: int | None = Query(default=None, ge=1),
    before_id: str | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    ensure_permission(current_user, "tasks.view")
    task = _require_clone_task(db, current_user, task_id)
    items = message_mapping_items(
        db, task, limit=limit,
        before_source_message_id=before_source_message_id,
        before_id=before_id,
    )
    return {
        "items": items,
        "next_cursor": ({
            "source_message_id": items[-1]["source_message_id"],
            "id": items[-1]["id"],
        } if len(items) == limit else None),
    }


@router.get("/{task_id}/clone-reconcile-cases")
def list_clone_reconcile_cases(
    task_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    before_created_at: datetime | None = None,
    before_id: str | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    ensure_permission(current_user, "tasks.view")
    task = _require_clone_task(db, current_user, task_id)
    items = reconcile_case_items(
        db, task, limit=limit,
        before_created_at=before_created_at, before_id=before_id,
    )
    return {
        "items": items,
        "next_cursor": ({
            "created_at": items[-1]["created_at"],
            "id": items[-1]["id"],
        } if len(items) == limit else None),
    }


@router.get("/{task_id}/clone-update-ingress-status")
def get_clone_update_ingress_status(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    ensure_permission(current_user, "tasks.view")
    task = _require_clone_task(db, current_user, task_id)
    return update_ingress_status(db, task)


@router.get("/{task_id}/clone-manual-reviews")
def list_clone_manual_reviews(
    task_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    after_sequencer_id: int | None = Query(default=None, ge=0),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    ensure_permission(current_user, "tasks.view")
    task = _require_clone_task(db, current_user, task_id)
    items = manual_review_items(
        db, task, limit=limit, after_sequencer_id=after_sequencer_id,
    )
    return {
        "items": items,
        "next_cursor": items[-1]["sequencer_id"] if len(items) == limit else None,
    }


@router.get("/{task_id}/clone-sequencer-head-cases")
def list_clone_sequencer_head_cases(
    task_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    before_created_at: datetime | None = None,
    before_id: str | None = None,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    ensure_permission(current_user, "tasks.view")
    _require_clone_task(db, current_user, task_id)
    stmt = select(CloneSequencerHeadCase).where(
        CloneSequencerHeadCase.task_id == task_id,
    )
    if before_created_at is not None:
        cursor_clause = CloneSequencerHeadCase.created_at < before_created_at
        if before_id:
            cursor_clause = or_(
                cursor_clause,
                and_(
                    CloneSequencerHeadCase.created_at == before_created_at,
                    CloneSequencerHeadCase.id < before_id,
                ),
            )
        stmt = stmt.where(cursor_clause)
    stmt = stmt.order_by(
        CloneSequencerHeadCase.created_at.desc(),
        CloneSequencerHeadCase.id.desc(),
    ).limit(limit)
    items = db.execute(stmt).scalars().all()
    return {
        "next_cursor": ({
            "created_at": items[-1].created_at.isoformat(),
            "id": items[-1].id,
        } if len(items) == limit else None),
        "items": [
            {
                "id": c.id,
                "sequencer_id": c.sequencer_id,
                "case_kind": c.case_kind,
                "state": c.state,
                "revision": c.revision,
                "remote_mutation_started": c.remote_mutation_started,
                "policy_snapshot": c.policy_snapshot,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in items
        ]
    }


@router.post("/{task_id}/clone-sequencer-head-cases/{case_id}/decision")
def decide_sequencer_head_case(
    task_id: str,
    case_id: str,
    *,
    req: GroupCloneSequencerHeadDecisionRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    """执行带 revision CAS 的 Sequencer 队头人工决策。"""
    ensure_permission(current_user, "tasks.manage")
    task = _require_clone_task(db, current_user, task_id)
    try:
        result = decide_clone_sequencer_case(
            db, task, case_id=case_id, request=req, actor_id=current_user.id,
        )
        db.commit()
        return result
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


def _require_clone_task(db: Session, current_user: CurrentUser, task_id: str) -> Task:
    try:
        return tenant_clone_task(db, current_user.tenant_id or 1, task_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
