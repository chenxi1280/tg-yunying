from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Lock

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AccountStatus,
    ChannelDiscussionGroupBinding,
    DiscussionMembershipFact,
    GroupAuthStatus,
    ListenerSourceState,
    Task,
    TaskSourceSubscription,
    TgAccount,
    TgGroup,
    TgGroupAccount,
)
from app.services._common import _now, audit
from app.services.account_usage_policy import apply_operational_account_filters
from app.services.group_listeners import collect_group_context

from .account_pool import select_task_accounts
from .account_scope import scoped_account_ids
from .channel_comment_discussion_contracts import membership_ready
from .channel_listener_runtime import drain_channel_listener_runtime
from .hard_hourly import enabled as hard_hourly_enabled
from .planner_wake import wake_task_planner
from .targets import group_from_reference
from .telegram_update_collector import drain_telegram_update_collector


_LOCK = Lock()
_RECENT_COLLECTS: dict[tuple[str, int], datetime] = {}
LISTENER_TASK_TYPES = {"group_ai_chat", "group_relay"}
LISTENER_TASK_STATUSES = {"pending", "running"}


@dataclass
class ListenerRuntimeSource:
    group_id: int
    tenant_id: int
    task_ids: list[str] = field(default_factory=list)
    account_ids: list[int] = field(default_factory=list)
    task_account_ids: dict[str, list[int]] = field(default_factory=dict)


@dataclass
class ListenerRuntimeDrainResult:
    source_count: int = 0
    collected_count: int = 0
    skipped_count: int = 0
    recovered_count: int = 0
    error_count: int = 0

    @property
    def processed_count(self) -> int:
        return self.collected_count + self.recovered_count + self.error_count


def should_collect_listener(object_type: str, object_id: int, *, window_seconds: int = 30) -> bool:
    key = (object_type, int(object_id))
    now_value = _now()
    window = timedelta(seconds=max(1, int(window_seconds or 30)))
    with _LOCK:
        last_collect = _RECENT_COLLECTS.get(key)
        if last_collect and last_collect + window > now_value:
            return False
        _RECENT_COLLECTS[key] = now_value
        return True


def invalidate_listener_collect(object_type: str, object_id: int) -> None:
    key = (object_type, int(object_id))
    with _LOCK:
        _RECENT_COLLECTS.pop(key, None)


def reset_listener_runtime_cache() -> None:
    with _LOCK:
        _RECENT_COLLECTS.clear()


def drain_listener_runtime(session_factory, *, tenant_id: int | None = None, limit: int = 50) -> ListenerRuntimeDrainResult:
    with session_factory() as session:
        sources = _listener_sources(session, tenant_id=tenant_id, limit=limit)
        stream_bindings = _group_ai_stream_bindings(session, sources)
        stream_setup_errors = _ensure_group_ai_streams(session, stream_bindings)
        comment_bindings = _channel_comment_stream_bindings(
            session,
            tenant_id=tenant_id,
            limit=limit,
        )
        stream_setup_errors += _ensure_channel_comment_streams(
            session,
            comment_bindings,
        )
        session.commit()
    collector_result = drain_telegram_update_collector(
        session_factory,
        tenant_id=tenant_id,
        limit=limit,
    )
    result = ListenerRuntimeDrainResult(
        source_count=len(sources) + collector_result.source_count,
        collected_count=collector_result.batch_count + collector_result.reconciled_count,
        error_count=collector_result.error_count + stream_setup_errors,
    )
    _consume_group_ai_streams(session_factory, stream_bindings, result)
    _consume_channel_comment_streams(session_factory, comment_bindings, result)
    for source in sources:
        with session_factory() as session:
            _drain_listener_source(session, source, result)
    channel_result = drain_channel_listener_runtime(
        session_factory,
        tenant_id=tenant_id,
        limit=limit,
    )
    result.source_count += channel_result.source_count
    result.collected_count += channel_result.processed_count
    result.error_count += channel_result.error_count
    return result


def _channel_comment_stream_bindings(
    session: Session,
    *,
    tenant_id: int | None,
    limit: int,
) -> list[tuple[str, str, int]]:
    conditions = [
        Task.type == "channel_comment",
        Task.status.in_(LISTENER_TASK_STATUSES),
        Task.deleted_at.is_(None),
    ]
    if tenant_id is not None:
        conditions.append(Task.tenant_id == tenant_id)
    tasks = list(session.scalars(select(Task).where(*conditions).order_by(
        Task.priority,
        Task.created_at,
    ).limit(max(1, int(limit)))))
    return [
        binding
        for task in tasks
        if (binding := _channel_comment_stream_binding(session, task)) is not None
    ]


def _channel_comment_stream_binding(
    session: Session,
    task: Task,
) -> tuple[str, str, int] | None:
    config = task.type_config or {}
    if config.get("engagement_contract_version") != "unified_engagement_v1":
        return None
    target_id = _as_int(config.get("target_channel_id"))
    binding = session.scalar(select(ChannelDiscussionGroupBinding).where(
        ChannelDiscussionGroupBinding.tenant_id == task.tenant_id,
        ChannelDiscussionGroupBinding.channel_target_id == target_id,
        ChannelDiscussionGroupBinding.is_current.is_(True),
        ChannelDiscussionGroupBinding.binding_status == "active",
    ))
    subscription = session.scalar(select(TaskSourceSubscription).where(
        TaskSourceSubscription.task_id == task.id,
        TaskSourceSubscription.lifecycle_epoch == int(task.task_lifecycle_epoch or 1),
        TaskSourceSubscription.state == "active",
    ))
    state = (
        session.get(ListenerSourceState, subscription.listener_source_state_id)
        if subscription and subscription.listener_source_state_id else None
    )
    if binding is None or state is None or state.account_id is None:
        return None
    account_id = _comment_listener_account_id(
        session, task, binding, source_account_id=int(state.account_id),
    )
    return task.id, binding.id, account_id


def _comment_listener_account_id(
    session: Session,
    task: Task,
    binding: ChannelDiscussionGroupBinding,
    *,
    source_account_id: int,
) -> int:
    scoped_ids = scoped_account_ids(session, task)
    facts = session.scalars(select(DiscussionMembershipFact).where(
        DiscussionMembershipFact.tenant_id == task.tenant_id,
        DiscussionMembershipFact.account_id.in_(scoped_ids),
        DiscussionMembershipFact.discussion_peer_id == str(binding.discussion_peer_id),
        DiscussionMembershipFact.group_binding_id == binding.id,
        DiscussionMembershipFact.is_current.is_(True),
    )) if scoped_ids else []
    ready_ids = sorted(
        int(fact.account_id) for fact in facts if membership_ready(fact, _now())
    )
    stats = dict(task.stats or {})
    previous = int(stats.get("comment_update_stream_listener_account_id") or 0)
    peer_errors = dict(stats.get("telegram_update_channel_errors") or {})
    previous_failed = (
        stats.get("comment_update_stream_state") == "blocked"
        or str(binding.discussion_peer_id) in peer_errors
    )
    if previous in ready_ids and not previous_failed:
        return previous
    candidates = [account_id for account_id in ready_ids if account_id != previous]
    return candidates[0] if candidates else source_account_id


def _ensure_channel_comment_streams(
    session: Session,
    bindings: list[tuple[str, str, int]],
) -> int:
    from .channel_comment_update_stream import ensure_channel_comment_update_subscription

    errors = 0
    for task_id, binding_id, account_id in bindings:
        task = session.get(Task, task_id)
        binding = session.get(ChannelDiscussionGroupBinding, binding_id)
        if task is None or binding is None:
            continue
        try:
            with session.begin_nested():
                ensure_channel_comment_update_subscription(
                    session,
                    task,
                    binding,
                    listener_account_id=account_id,
                )
        except Exception as exc:  # noqa: BLE001 - isolate one discussion stream.
            _mark_channel_comment_stream_error(
                task, exc, account_id=account_id,
            )
            errors += 1
    return errors


def _consume_channel_comment_streams(
    session_factory,
    bindings: list[tuple[str, str, int]],
    result: ListenerRuntimeDrainResult,
) -> None:
    from .channel_comment_update_stream import consume_channel_comment_update_deliveries

    for task_id, binding_id, _account_id in bindings:
        with session_factory() as session:
            task = session.get(Task, task_id)
            binding = session.get(ChannelDiscussionGroupBinding, binding_id)
            if task is None or binding is None:
                continue
            try:
                changed = consume_channel_comment_update_deliveries(
                    session,
                    task,
                    binding,
                )
                result.collected_count += changed
                session.commit()
            except Exception as exc:  # noqa: BLE001 - isolate one discussion consumer.
                session.rollback()
                task = session.get(Task, task_id)
                if task is not None:
                    _mark_channel_comment_stream_error(task, exc)
                    session.commit()
                result.error_count += 1


def _mark_channel_comment_stream_error(
    task: Task,
    error: Exception,
    *,
    account_id: int | None = None,
) -> None:
    detail = str(error).strip() or type(error).__name__
    previous = int(
        (task.stats or {}).get("comment_update_stream_listener_account_id") or 0
    )
    task.stats = {
        **dict(task.stats or {}),
        "comment_update_stream_state": "blocked",
        "comment_update_stream_error": detail,
        "comment_update_stream_listener_account_id": (
            int(account_id) if account_id is not None else previous
        ),
    }


def _group_ai_stream_bindings(
    session: Session,
    sources: list[ListenerRuntimeSource],
) -> list[tuple[str, int, int]]:
    bindings: list[tuple[str, int, int]] = []
    for source in sources:
        for task_id in source.task_ids:
            task = session.get(Task, task_id)
            if task is None or task.type != "group_ai_chat":
                continue
            match = source.task_account_ids.get(task.id)
            if match:
                bindings.append((task.id, source.group_id, int(match[0])))
    return bindings


def _ensure_group_ai_streams(
    session: Session,
    bindings: list[tuple[str, int, int]],
) -> int:
    from .group_ai_update_stream import ensure_group_ai_update_subscription

    error_count = 0
    for task_id, group_id, account_id in bindings:
        task = session.get(Task, task_id)
        group = session.get(TgGroup, group_id)
        if task is None or group is None:
            continue
        try:
            with session.begin_nested():
                ensure_group_ai_update_subscription(
                    session, task, group, listener_account_id=account_id,
                )
        except Exception as exc:  # noqa: BLE001 - isolate one stream setup.
            _mark_group_ai_stream_error(task, exc)
            error_count += 1
    return error_count


def _consume_group_ai_streams(
    session_factory,
    bindings: list[tuple[str, int, int]],
    result: ListenerRuntimeDrainResult,
) -> None:
    from .group_ai_update_stream import consume_group_ai_update_deliveries

    for task_id, group_id, _account_id in bindings:
        with session_factory() as session:
            task = session.get(Task, task_id)
            group = session.get(TgGroup, group_id)
            if task is None or group is None:
                continue
            try:
                inserted = consume_group_ai_update_deliveries(
                    session, task, group,
                )
                result.collected_count += inserted
                session.commit()
            except Exception as exc:  # noqa: BLE001 - isolate one stream consumer.
                session.rollback()
                task = session.get(Task, task_id)
                if task is not None:
                    _mark_group_ai_stream_error(task, exc)
                    session.commit()
                result.error_count += 1


def _mark_group_ai_stream_error(task: Task, error: Exception) -> None:
    detail = str(error).strip() or type(error).__name__
    task.stats = {
        **dict(task.stats or {}),
        "group_update_stream_state": "blocked",
        "group_update_stream_error": detail,
    }


def _listener_sources(session: Session, *, tenant_id: int | None = None, limit: int = 50) -> list[ListenerRuntimeSource]:
    conditions = [
        Task.type.in_(LISTENER_TASK_TYPES),
        Task.status.in_(LISTENER_TASK_STATUSES),
        Task.deleted_at.is_(None),
    ]
    if tenant_id is not None:
        conditions.append(Task.tenant_id == tenant_id)
    tasks = list(
        session.scalars(
            select(Task)
            .where(*conditions)
            .order_by(Task.priority.asc(), Task.next_run_at.asc().nullsfirst(), Task.created_at.asc())
            .limit(max(1, limit * 3))
        )
    )
    sources: dict[int, ListenerRuntimeSource] = {}
    for task in tasks:
        for group_id, account_ids in _task_listener_groups(session, task):
            group = session.get(TgGroup, group_id)
            if not group or group.tenant_id != task.tenant_id or group.auth_status != GroupAuthStatus.AUTHORIZED.value:
                continue
            source = sources.setdefault(group.id, ListenerRuntimeSource(group_id=group.id, tenant_id=group.tenant_id))
            if task.id not in source.task_ids:
                source.task_ids.append(task.id)
            source.task_account_ids[task.id] = list(account_ids)
            for account_id in account_ids:
                if account_id not in source.account_ids:
                    source.account_ids.append(account_id)
            if len(sources) >= limit:
                return list(sources.values())
    return list(sources.values())


def _task_listener_groups(session: Session, task: Task) -> list[tuple[int, list[int]]]:
    config = task.type_config or {}
    if task.type == "group_ai_chat":
        group = group_from_reference(
            session,
            task.tenant_id,
            group_id=_as_int(config.get("target_group_id")),
            operation_target_id=_as_int(config.get("target_operation_target_id")),
        )
        if not group:
            return []
        group_id = group.id
        history_fetch_account_id = _as_int(config.get("history_fetch_account_id"))
        if history_fetch_account_id:
            return [(group_id, [history_fetch_account_id])]
        listener_account_config = {**(task.account_config or {}), "cooldown_per_account_minutes": 0}
        accounts = select_task_accounts(
            session,
            task.tenant_id,
            listener_account_config,
            target_group_id=group.id,
            limit=1,
            enforce_capacity=False,
        )
        return [(group_id, [account.id for account in accounts])]
    if task.type != "group_relay":
        return []
    configured_monitor_ids = _as_int_list(config.get("monitor_account_ids"))
    groups: list[tuple[int, list[int]]] = []
    for item in config.get("source_groups") or []:
        if not isinstance(item, dict) or not item.get("is_active", True):
            continue
        group = group_from_reference(
            session,
            task.tenant_id,
            group_id=_as_int(item.get("group_id")),
            operation_target_id=_as_int(item.get("operation_target_id")),
        )
        if not group:
            continue
        group_id = group.id
        account_ids = configured_monitor_ids or _source_group_account_ids(session, task.tenant_id, group_id)
        groups.append((group_id, account_ids))
    return groups


def _drain_listener_source(session: Session, source: ListenerRuntimeSource, result: ListenerRuntimeDrainResult) -> None:
    group = session.get(TgGroup, source.group_id)
    if not group or group.tenant_id != source.tenant_id or group.auth_status != GroupAuthStatus.AUTHORIZED.value:
        result.skipped_count += 1
        return
    if not _claim_listener_source(session, source, group):
        result.skipped_count += 1
        return
    account_ids = _usable_group_account_ids(session, group, source.account_ids)
    if not account_ids:
        _mark_listener_runtime_error(
            session, group=group, task_ids=source.task_ids, detail="没有可用监听账号",
        )
        _update_listener_source(session, source, group, occurred_at=_now(), error="没有可用监听账号")
        result.error_count += 1
        session.commit()
        return
    account_ids = account_ids[:1]
    if not should_collect_listener("group", group.id, window_seconds=group.listener_interval_seconds):
        result.skipped_count += 1
        return
    recovered = _recover_group_listener_account(session, group, account_ids[0])
    session.commit()
    try:
        inserted = collect_group_context(
            session,
            group,
            account_ids,
            create_source_media=_source_has_relay_task(session, source.task_ids),
            learning_scene="group_chat" if _source_has_ai_learning_task(session, source.task_ids) else None,
        )
    except Exception as exc:  # noqa: BLE001 - keep other listener sources draining.
        session.rollback()
        group = session.get(TgGroup, source.group_id)
        if group:
            _mark_listener_runtime_error(
                session, group=group, task_ids=source.task_ids, detail=str(exc),
            )
            _update_listener_source(session, source, group, occurred_at=_now(), error=str(exc))
            session.commit()
        result.error_count += 1
        return
    now_value = _now()
    group.listener_enabled = True
    group.listener_last_polled_at = now_value
    group.listener_last_error = ""
    _update_listener_source(session, source, group, occurred_at=now_value, error="")
    _mark_listener_runtime_success(
        session,
        task_ids=source.task_ids,
        group_id=group.id,
        inserted=inserted,
        occurred_at=now_value,
    )
    if recovered:
        result.recovered_count += 1
    result.collected_count += int(inserted or 0)
    session.commit()


def _recover_group_listener_account(session: Session, group: TgGroup, account_id: int) -> bool:
    active_listener_stmt = (
        select(TgGroupAccount.id)
        .join(TgAccount, TgAccount.id == TgGroupAccount.account_id)
        .where(
            TgGroupAccount.tenant_id == group.tenant_id,
            TgGroupAccount.group_id == group.id,
            TgGroupAccount.is_listener.is_(True),
            TgAccount.status == AccountStatus.ACTIVE.value,
            TgAccount.deleted_at.is_(None),
        )
        .limit(1)
    )
    active_listener_count = session.scalar(apply_operational_account_filters(active_listener_stmt))
    if active_listener_count and not group.listener_last_error:
        return False
    link = session.scalar(
        select(TgGroupAccount).where(
            TgGroupAccount.tenant_id == group.tenant_id,
            TgGroupAccount.group_id == group.id,
            TgGroupAccount.account_id == account_id,
            TgGroupAccount.can_send.is_(True),
        )
    )
    if not link or link.is_listener:
        return False
    link.is_listener = True
    audit(
        session,
        tenant_id=group.tenant_id,
        actor="监听运行层",
        action="自动恢复监听账号",
        target_type="tg_group",
        target_id=str(group.id),
        detail=f"account={account_id}",
    )
    return True


def _source_has_relay_task(session: Session, task_ids: list[str]) -> bool:
    return bool(task_ids and session.scalar(select(Task.id).where(Task.id.in_(task_ids), Task.type == "group_relay").limit(1)))


def _source_has_ai_learning_task(session: Session, task_ids: list[str]) -> bool:
    return bool(task_ids and session.scalar(select(Task.id).where(Task.id.in_(task_ids), Task.type == "group_ai_chat").limit(1)))


def _claim_listener_source(session: Session, source: ListenerRuntimeSource, group: TgGroup) -> bool:
    now_value = _now()
    account_id = source.account_ids[0] if source.account_ids else None
    owner = _listener_owner()
    state = session.scalar(
        select(ListenerSourceState).where(
            ListenerSourceState.tenant_id == source.tenant_id,
            ListenerSourceState.source_type == "group",
            ListenerSourceState.source_peer_id == str(group.id),
            ListenerSourceState.account_id == account_id,
        )
    )
    if state and state.lease_owner and state.lease_owner != owner and state.lease_expires_at and _naive_datetime(state.lease_expires_at) > _naive_datetime(now_value):
        return False
    if not state:
        state = ListenerSourceState(
            tenant_id=source.tenant_id,
            source_type="group",
            source_peer_id=str(group.id),
            account_id=account_id,
            shard_key=f"group:{group.id}",
            collect_window_seconds=int(group.listener_interval_seconds or 30),
        )
        session.add(state)
    state.lease_owner = owner
    state.lease_expires_at = now_value + timedelta(seconds=max(30, int(group.listener_interval_seconds or 30) * 2))
    state.updated_at = now_value
    session.commit()
    return True


def _update_listener_source(session: Session, source: ListenerRuntimeSource, group: TgGroup, *, occurred_at: datetime, error: str) -> None:
    account_id = source.account_ids[0] if source.account_ids else None
    state = session.scalar(
        select(ListenerSourceState).where(
            ListenerSourceState.tenant_id == source.tenant_id,
            ListenerSourceState.source_type == "group",
            ListenerSourceState.source_peer_id == str(group.id),
            ListenerSourceState.account_id == account_id,
        )
    )
    if not state:
        return
    state.last_event_at = occurred_at
    state.last_error = error
    state.lease_owner = ""
    state.lease_expires_at = None
    state.updated_at = occurred_at


def _listener_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:listener"


def _mark_listener_runtime_success(
    session: Session,
    *,
    task_ids: list[str],
    group_id: int,
    inserted: int,
    occurred_at: datetime,
) -> None:
    occurred_at = _naive_datetime(occurred_at)
    for task_id in task_ids:
        task = session.get(Task, task_id)
        if not task:
            continue
        stats = dict(task.stats or {})
        stats["listener_runtime_last_collect_at"] = occurred_at.isoformat()
        stats["listener_runtime_last_source_group_id"] = group_id
        stats["listener_runtime_last_collect_count"] = int(inserted or 0)
        stats.pop("listener_runtime_last_error", None)
        if inserted > 0:
            stats.pop("idle_continuation_next_run_at", None)
            if task.type == "group_ai_chat" and hard_hourly_enabled(task):
                stats["hard_hourly_next_check_at"] = occurred_at.isoformat()
                task.hard_hourly_next_check_at = occurred_at
        task.stats = stats
        next_run_at = _naive_datetime(task.next_run_at)
        if inserted > 0 and (next_run_at is None or next_run_at > occurred_at):
            task.next_run_at = occurred_at
            task.updated_at = occurred_at
        if inserted > 0:
            wake_task_planner(
                session,
                task,
                reason_code="group_context_inserted",
                not_before_at=occurred_at,
            )
        if task.last_error and ("监听" in task.last_error or "上下文" in task.last_error):
            task.last_error = ""


def _mark_listener_runtime_error(
    session: Session,
    *,
    group: TgGroup,
    task_ids: list[str],
    detail: str,
) -> None:
    now_value = _now()
    group.listener_enabled = True
    group.listener_last_polled_at = now_value
    group.listener_last_error = detail
    for task_id in task_ids:
        task = session.get(Task, task_id)
        if task:
            stats = dict(task.stats or {})
            stats["listener_runtime_last_error"] = detail
            stats["listener_runtime_last_source_group_id"] = group.id
            stats["listener_runtime_last_collect_at"] = now_value.isoformat()
            task.stats = stats


def _naive_datetime(value):
    if value and getattr(value, "tzinfo", None):
        return value.replace(tzinfo=None)
    return value


def _source_group_account_ids(session: Session, tenant_id: int, group_id: int) -> list[int]:
    stmt = apply_operational_account_filters(
        select(TgAccount.id)
        .join(TgGroupAccount, TgGroupAccount.account_id == TgAccount.id)
        .where(
            TgGroupAccount.tenant_id == tenant_id,
            TgGroupAccount.group_id == group_id,
            TgGroupAccount.can_send.is_(True),
            TgAccount.tenant_id == tenant_id,
            TgAccount.status == AccountStatus.ACTIVE.value,
            TgAccount.deleted_at.is_(None),
        )
        .order_by(TgGroupAccount.is_listener.desc(), TgAccount.health_score.desc(), TgAccount.id.asc())
    )
    return list(session.scalars(stmt))


def _usable_group_account_ids(session: Session, group: TgGroup, account_ids: list[int]) -> list[int]:
    candidate_ids = list(dict.fromkeys(account_ids))
    if not candidate_ids:
        return []
    rows = session.scalars(
        apply_operational_account_filters(
            select(TgAccount.id)
            .join(TgGroupAccount, TgGroupAccount.account_id == TgAccount.id)
            .where(
                TgAccount.tenant_id == group.tenant_id,
                TgAccount.id.in_(candidate_ids),
                TgAccount.status == AccountStatus.ACTIVE.value,
                TgAccount.deleted_at.is_(None),
                TgGroupAccount.tenant_id == group.tenant_id,
                TgGroupAccount.group_id == group.id,
            )
        )
    )
    valid = set(rows)
    return [account_id for account_id in candidate_ids if account_id in valid]


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_int_list(value) -> list[int]:
    if not value:
        return []
    if isinstance(value, str):
        raw_items = [item.strip() for item in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = [value]
    items: list[int] = []
    for item in raw_items:
        parsed = _as_int(item)
        if parsed is not None and parsed not in items:
            items.append(parsed)
    return items


__all__ = [
    "ListenerRuntimeDrainResult",
    "drain_listener_runtime",
    "invalidate_listener_collect",
    "reset_listener_runtime_cache",
    "should_collect_listener",
]
