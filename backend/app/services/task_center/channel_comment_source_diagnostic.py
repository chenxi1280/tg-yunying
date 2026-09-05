from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session
from app.timezone import as_beijing, as_beijing_aware

from app.models import (
    ChannelMessage,
    ChannelMessageSourceRevision,
    ListenerSourceState,
    OperationTarget,
    Task,
    TaskSourceSubscription,
    TgAccount,
)


@dataclass(frozen=True)
class LatestSourceDiagnosticRequest:
    tenant_id: int
    task_id: str


@dataclass(frozen=True)
class LatestSourceDiagnosticDependencies:
    fetch_messages: Callable
    credentials_for_account: Callable
    observed_at: datetime


@dataclass(frozen=True)
class _DiagnosticContext:
    task: Task
    channel: OperationTarget
    subscription: TaskSourceSubscription
    state: ListenerSourceState
    account: TgAccount


def diagnose_latest_channel_source(
    session: Session,
    request: LatestSourceDiagnosticRequest,
    dependencies: LatestSourceDiagnosticDependencies,
) -> dict:
    context, blocker = _diagnostic_context(session, request)
    if blocker:
        return _result(blocker, request, context=context)
    if _listener_lease_active(context.state, dependencies.observed_at):
        return _result("listener_session_in_use", request, context=context)
    try:
        snapshots = dependencies.fetch_messages(
            context.account.id, context.channel.tg_peer_id,
            context.account.session_ciphertext,
            dependencies.credentials_for_account(context.account, context.task.type),
            limit=1,
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic exposes a typed read failure.
        return _result(
            "diagnostic_probe_failed", request, context=context,
            error_code=type(exc).__name__,
        )
    return _compare_latest(
        session, request, context=context, snapshots=snapshots,
    )


def _diagnostic_context(
    session: Session,
    request: LatestSourceDiagnosticRequest,
) -> tuple[_DiagnosticContext | None, str]:
    task = session.scalar(select(Task).where(
        Task.id == request.task_id, Task.tenant_id == request.tenant_id,
    ))
    if task is None or task.type != "channel_comment":
        return None, "channel_comment_task_not_found"
    target_id = int((task.type_config or {}).get("target_channel_id") or 0)
    channel = session.get(OperationTarget, target_id) if target_id else None
    subscription = session.scalar(select(TaskSourceSubscription).where(
        TaskSourceSubscription.task_id == task.id,
        TaskSourceSubscription.lifecycle_epoch == task.task_lifecycle_epoch,
    ).order_by(TaskSourceSubscription.created_at.desc()))
    state = session.get(ListenerSourceState, subscription.listener_source_state_id) if subscription else None
    account = session.get(TgAccount, state.account_id) if state and state.account_id else None
    if channel is None:
        return None, "channel_target_missing"
    if subscription is None or state is None:
        return None, "canonical_listener_state_missing"
    if account is None or not account.session_ciphertext:
        return None, "canonical_listener_account_unavailable"
    return _DiagnosticContext(task, channel, subscription, state, account), ""


def _listener_lease_active(state: ListenerSourceState, observed_at: datetime) -> bool:
    return bool(
        state.lease_owner and state.lease_expires_at
        and _wall(state.lease_expires_at) > _wall(observed_at)
    )


def _compare_latest(
    session: Session,
    request: LatestSourceDiagnosticRequest,
    *,
    context: _DiagnosticContext,
    snapshots,
) -> dict:
    remote = next(iter(snapshots or []), None)
    local = _latest_local_source(session, context)
    if remote is None:
        return _result("telegram_channel_empty", request, context=context, local=local)
    remote_identity = _remote_identity(context, remote)
    if local is None:
        return _result(
            "listener_lag", request, context=context,
            remote=remote_identity, local=None,
        )
    local_identity = _local_identity(context, local)
    state = "in_sync" if remote_identity == local_identity and context.state.snapshot_status == "ready" else "listener_lag"
    return _result(
        state, request, context=context,
        remote=remote_identity, local=local_identity,
    )


def _latest_local_source(session: Session, context: _DiagnosticContext):
    return session.scalar(
        select(ChannelMessageSourceRevision)
        .join(ChannelMessage, ChannelMessage.current_source_revision_id == ChannelMessageSourceRevision.id)
        .where(
            ChannelMessage.tenant_id == context.task.tenant_id,
            ChannelMessage.channel_target_id == context.channel.id,
        )
        .order_by(ChannelMessage.published_at.desc(), ChannelMessage.message_id.desc())
    )


def _remote_identity(context: _DiagnosticContext, snapshot) -> dict:
    text = str(getattr(snapshot, "content_text", "") or snapshot.content_preview or "")
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {
        "channel_tg_peer_id": str(context.channel.tg_peer_id),
        "source_message_id": int(snapshot.message_id),
        "source_published_at": _iso(snapshot.published_at),
        "telegram_edit_date": _iso(getattr(snapshot, "edited_at", None)),
        "source_content_hash": text_hash,
    }


def _local_identity(context: _DiagnosticContext, source: ChannelMessageSourceRevision) -> dict:
    return {
        "channel_tg_peer_id": str(context.channel.tg_peer_id),
        "source_message_id": int(source.source_remote_message_id),
        "source_published_at": _iso(source.source_published_at),
        "telegram_edit_date": _iso(source.telegram_edit_date),
        "source_content_hash": str(source.source_content_hash),
    }


def _result(state: str, request: LatestSourceDiagnosticRequest, **values) -> dict:
    context = values.pop("context", None)
    listener = context.state if context else None
    return {
        "state": state, "tenant_id": request.tenant_id, "task_id": request.task_id,
        "listener_snapshot_state": listener.snapshot_status if listener else "unavailable",
        "listener_account_id": listener.account_id if listener else None,
        "last_collected_at": _iso(listener.observed_at) if listener else None,
        **values,
    }


def _iso(value: datetime | None) -> str | None:
    return as_beijing_aware(value).isoformat() if value else None


def _wall(value: datetime) -> datetime:
    return as_beijing(value)


__all__ = [
    "LatestSourceDiagnosticDependencies", "LatestSourceDiagnosticRequest",
    "diagnose_latest_channel_source",
]
