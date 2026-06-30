from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AccountStatus, Task, TgAccount, TgAccountOnlineState, TgGroup, TgGroupAccount
from app.services._common import _now
from app.services.account_online_probe import probe_due_online_states, stale_deadline_for_state
from app.timezone import as_beijing

ONLINE_TASK_STATUSES = {"running", "paused"}
ONLINE_AVAILABLE_STATUSES = {"online", "warming", "recovering"}
LOW_FREQUENCY_KEEPALIVE = "low_frequency"

def reconcile_account_online_sources(
    session: Session,
    *,
    tenant_id: int,
    sources: list[dict[str, Any]],
    now: datetime | None = None,
) -> int:
    current_time = now or _now()
    desired = _desired_accounts_from_sources(sources)
    rows = _states_by_account(session, tenant_id)
    changed = 0
    for account_id, meta in desired.items():
        state = rows.get(account_id) or TgAccountOnlineState(tenant_id=tenant_id, account_id=account_id)
        changed += _apply_desired_state(state, meta, current_time)
        session.add(state)
        rows[account_id] = state
    for account_id, state in rows.items():
        if account_id not in desired and state.desired_online:
            changed += _clear_desired_state(state, current_time)
    return changed


def is_account_online_ready(
    session: Session,
    *,
    tenant_id: int,
    account_id: int,
    now: datetime | None = None,
) -> bool:
    current_time = now or _now()
    state = _account_online_state(session, tenant_id, account_id)
    return _state_is_ready(state, current_time)


def is_account_online_ready_for_planning(
    session: Session,
    *,
    tenant_id: int,
    account_id: int,
    now: datetime | None = None,
) -> bool:
    current_time = now or _now()
    state = _account_online_state(session, tenant_id, account_id)
    return _state_is_ready(state, current_time)


def is_account_online_available(
    session: Session,
    *,
    tenant_id: int,
    account_id: int,
    now: datetime | None = None,
) -> bool:
    current_time = now or _now()
    state = _account_online_state(session, tenant_id, account_id)
    return _state_is_available(state, current_time)


def _account_online_state(session: Session, tenant_id: int, account_id: int) -> TgAccountOnlineState | None:
    state = session.scalar(
        select(TgAccountOnlineState).where(
            TgAccountOnlineState.tenant_id == tenant_id,
            TgAccountOnlineState.account_id == account_id,
        )
    )
    return state


def drain_account_online_keepalive(session_factory, limit: int = 100) -> int:
    from app.services.task_center.heartbeat import record_worker_heartbeat

    with session_factory() as session:
        record_worker_heartbeat(session, process_type="account-online", metadata={"limit": limit})
        reconcile_runtime_online_sources(session)
        processed = probe_due_online_states(session, limit=limit)
        processed += mark_stale_online_states(session, limit=limit)
        session.commit()
        return processed


def reconcile_runtime_online_sources(
    session: Session,
    *,
    tenant_id: int | None = None,
    include_global: bool | None = None,
    now: datetime | None = None,
) -> int:
    current_time = now or _now()
    global_enabled = get_settings().enable_global_account_online_keepalive if include_global is None else include_global
    changed = 0
    for current_tenant_id in _runtime_tenant_ids(session, tenant_id):
        sources: list[dict[str, Any]] = []
        if global_enabled:
            sources.extend(_global_keepalive_sources(session, current_tenant_id))
        sources.extend(_listener_keepalive_sources(session, current_tenant_id))
        sources.extend(_running_task_keepalive_sources(session, current_tenant_id))
        changed += reconcile_account_online_sources(
            session,
            tenant_id=current_tenant_id,
            sources=_dedupe_source_refs(sources),
            now=current_time,
        )
    return changed


def mark_stale_online_states(session: Session, *, limit: int = 100, now: datetime | None = None) -> int:
    current_time = now or _now()
    rows = list(
        session.scalars(
            select(TgAccountOnlineState)
            .where(
                TgAccountOnlineState.desired_online.is_(True),
                TgAccountOnlineState.online_status == "online",
                TgAccountOnlineState.stale_after_at.is_not(None),
                TgAccountOnlineState.stale_after_at <= current_time,
            )
            .order_by(TgAccountOnlineState.stale_after_at.asc())
            .limit(max(1, limit))
        )
    )
    for state in rows:
        state.online_status = "offline"
        state.failure_type = "stale_probe"
        state.failure_detail = "在线状态超过 stale_after_at 未刷新"
        state.next_probe_at = current_time
        state.updated_at = current_time
    return len(rows)


def _runtime_tenant_ids(session: Session, tenant_id: int | None) -> list[int]:
    if tenant_id is not None:
        return [int(tenant_id)]
    account_tenants = select(TgAccount.tenant_id).where(TgAccount.deleted_at.is_(None))
    task_tenants = select(Task.tenant_id).where(Task.deleted_at.is_(None), Task.status.in_(ONLINE_TASK_STATUSES))
    group_tenants = select(TgGroup.tenant_id).where(TgGroup.listener_enabled.is_(True))
    tenant_ids = set(session.scalars(account_tenants))
    tenant_ids.update(session.scalars(task_tenants))
    tenant_ids.update(session.scalars(group_tenants))
    return sorted(int(item) for item in tenant_ids if item is not None)


def _global_keepalive_sources(session: Session, tenant_id: int) -> list[dict[str, Any]]:
    return [
        _source_for_account("global", "global_keepalive", account)
        for account in _active_session_accounts(session, tenant_id)
    ]


def _listener_keepalive_sources(session: Session, tenant_id: int) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    groups = session.scalars(
        select(TgGroup).where(TgGroup.tenant_id == tenant_id, TgGroup.listener_enabled.is_(True))
    )
    for group in groups:
        accounts = _listener_accounts_for_group(session, tenant_id, group.id)
        sources.extend(_source_for_account("listener", f"group:{group.id}", account) for account in accounts)
    return sources


def _running_task_keepalive_sources(session: Session, tenant_id: int) -> list[dict[str, Any]]:
    tasks = session.scalars(
        select(Task)
        .where(Task.tenant_id == tenant_id, Task.deleted_at.is_(None), Task.status.in_(ONLINE_TASK_STATUSES))
        .order_by(Task.created_at.asc(), Task.id.asc())
    )
    sources: list[dict[str, Any]] = []
    for task in tasks:
        if task.type == "group_ai_chat":
            sources.extend(_group_ai_task_sources(session, task))
        elif task.type == "group_relay":
            sources.extend(_group_relay_task_sources(session, task))
    return sources


def _group_ai_task_sources(session: Session, task: Task) -> list[dict[str, Any]]:
    config = task.type_config if isinstance(task.type_config, dict) else {}
    target_group_id = _as_int(config.get("target_group_id"))
    accounts = _configured_online_accounts(session, task.tenant_id, task.account_config or {}, target_group_id)
    sources = [_task_source_for_account(task, task.id, account) for account in accounts]
    history_account = _active_session_account(session, task.tenant_id, _as_int(config.get("history_fetch_account_id")))
    if history_account:
        sources.append(_task_source_for_account(task, f"{task.id}:history", history_account))
    return sources


def _group_relay_task_sources(session: Session, task: Task) -> list[dict[str, Any]]:
    config = task.type_config if isinstance(task.type_config, dict) else {}
    sources: list[dict[str, Any]] = []
    for source_id in _relay_source_group_ids(config):
        monitor_accounts = _relay_monitor_accounts(session, task, config, source_id)
        sources.extend(_task_source_for_account(task, f"{task.id}:source:{source_id}", account) for account in monitor_accounts)
    for target_id in _relay_target_group_ids(config):
        accounts = _relay_target_accounts(session, task, config, target_id)
        sources.extend(_task_source_for_account(task, f"{task.id}:target:{target_id}", account) for account in accounts)
    return sources


def _desired_accounts_from_sources(sources: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    desired: dict[int, dict[str, Any]] = {}
    for source in sources:
        for account_id in source.get("account_ids", []):
            meta = desired.setdefault(int(account_id), {"sources": [], "active_task_count": 0})
            meta["sources"].append(_source_ref(source))
            if source.get("source_type") == "task" and source.get("keepalive_mode") != LOW_FREQUENCY_KEEPALIVE:
                meta["active_task_count"] += 1
            _copy_probe_meta(meta, source)
    return desired


def _configured_online_accounts(
    session: Session,
    tenant_id: int,
    account_config: dict[str, Any],
    target_group_id: int | None = None,
) -> list[TgAccount]:
    mode = account_config.get("selection_mode") or ("manual" if account_config.get("account_ids") else "all")
    stmt = _active_session_account_stmt(tenant_id)
    if mode == "manual":
        account_ids = _as_int_list(account_config.get("account_ids"))
        if not account_ids:
            return []
        rows = _accounts_for_stmt(session, stmt.where(TgAccount.id.in_(account_ids)), target_group_id)
        by_id = {account.id: account for account in rows}
        return [by_id[account_id] for account_id in account_ids if account_id in by_id]
    if mode == "group":
        pool_id = _as_int(account_config.get("account_group_id"))
        if not pool_id:
            return []
        stmt = stmt.where(TgAccount.pool_id == pool_id)
    return _accounts_for_stmt(session, stmt.order_by(TgAccount.health_score.desc(), TgAccount.id.asc()), target_group_id)


def _relay_monitor_accounts(session: Session, task: Task, config: dict[str, Any], source_group_id: int) -> list[TgAccount]:
    configured_ids = _as_int_list(config.get("monitor_account_ids"))
    if configured_ids:
        return _accounts_by_ids(session, task.tenant_id, configured_ids)
    listener_accounts = _listener_accounts_for_group(session, task.tenant_id, source_group_id)
    if listener_accounts:
        return listener_accounts
    return _group_accounts(session, task.tenant_id, source_group_id, require_send=False)


def _relay_target_accounts(session: Session, task: Task, config: dict[str, Any], target_group_id: int) -> list[TgAccount]:
    account_config = dict(task.account_config or {})
    strategy = config.get("account_strategy") if isinstance(config.get("account_strategy"), dict) else {}
    account_ids = _as_int_list(config.get("send_account_ids") or strategy.get("account_ids") or strategy.get("send_account_ids"))
    if account_ids:
        account_config["selection_mode"] = "manual"
        account_config["account_ids"] = account_ids
    return _configured_online_accounts(session, task.tenant_id, account_config, target_group_id)


def _relay_source_group_ids(config: dict[str, Any]) -> list[int]:
    ids: list[int] = []
    for item in config.get("source_groups") or []:
        if not isinstance(item, dict) or item.get("is_active") is False:
            continue
        group_id = _as_int(item.get("group_id"))
        if group_id and group_id not in ids:
            ids.append(group_id)
    return ids


def _relay_target_group_ids(config: dict[str, Any]) -> list[int]:
    ids = _as_int_list(config.get("target_group_ids"))
    ids.extend(item for item in _as_int_list(config.get("target_group_id")) if item not in ids)
    routing = config.get("routing") if isinstance(config.get("routing"), dict) else {}
    for item in _as_int_list(routing.get("default_target_group_ids") or routing.get("target_group_ids")):
        if item not in ids:
            ids.append(item)
    return ids


def _listener_accounts_for_group(session: Session, tenant_id: int, group_id: int) -> list[TgAccount]:
    return _group_accounts(session, tenant_id, group_id, require_listener=True)


def _group_accounts(
    session: Session,
    tenant_id: int,
    group_id: int,
    *,
    require_send: bool = False,
    require_listener: bool = False,
) -> list[TgAccount]:
    stmt = _active_session_account_stmt(tenant_id).join(TgGroupAccount, TgGroupAccount.account_id == TgAccount.id).where(
        TgGroupAccount.tenant_id == tenant_id,
        TgGroupAccount.group_id == group_id,
    )
    if require_send:
        stmt = stmt.where(TgGroupAccount.can_send.is_(True))
    if require_listener:
        stmt = stmt.where(TgGroupAccount.is_listener.is_(True))
    return list(session.scalars(stmt.order_by(TgGroupAccount.id.asc())))


def _accounts_for_stmt(session: Session, stmt, target_group_id: int | None) -> list[TgAccount]:
    if target_group_id:
        stmt = stmt.join(TgGroupAccount, TgGroupAccount.account_id == TgAccount.id).where(
            TgGroupAccount.group_id == target_group_id,
            TgGroupAccount.can_send.is_(True),
        )
    return list(session.scalars(stmt))


def _accounts_by_ids(session: Session, tenant_id: int, account_ids: list[int]) -> list[TgAccount]:
    rows = _accounts_for_stmt(session, _active_session_account_stmt(tenant_id).where(TgAccount.id.in_(account_ids)), None)
    by_id = {account.id: account for account in rows}
    return [by_id[account_id] for account_id in account_ids if account_id in by_id]


def _active_session_accounts(session: Session, tenant_id: int) -> list[TgAccount]:
    return list(session.scalars(_active_session_account_stmt(tenant_id).order_by(TgAccount.id.asc())))


def _active_session_account(session: Session, tenant_id: int, account_id: int | None) -> TgAccount | None:
    if not account_id:
        return None
    return session.scalar(_active_session_account_stmt(tenant_id).where(TgAccount.id == account_id).limit(1))


def _active_session_account_stmt(tenant_id: int):
    return select(TgAccount).where(
        TgAccount.tenant_id == tenant_id,
        TgAccount.deleted_at.is_(None),
        TgAccount.status == AccountStatus.ACTIVE.value,
        TgAccount.session_ciphertext.is_not(None),
        TgAccount.session_ciphertext != "",
    )


def _source_for_account(source_type: str, source_id: str, account: TgAccount) -> dict[str, Any]:
    return {
        "source_type": source_type,
        "source_id": source_id,
        "account_ids": [account.id],
        "session_kind": "primary",
        "session_id": str(account.id),
        "proxy_id": account.proxy_id,
    }


def _task_source_for_account(task: Task, source_id: str, account: TgAccount) -> dict[str, Any]:
    source = _source_for_account("task", source_id, account)
    if task.status == "paused":
        source["keepalive_mode"] = LOW_FREQUENCY_KEEPALIVE
    return source


def _dedupe_source_refs(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, int]] = set()
    result: list[dict[str, Any]] = []
    for source in sources:
        account_ids = [int(account_id) for account_id in source.get("account_ids", [])]
        unique_ids = [account_id for account_id in account_ids if (source["source_type"], source["source_id"], account_id) not in seen]
        for account_id in unique_ids:
            seen.add((source["source_type"], source["source_id"], account_id))
        if unique_ids:
            result.append({**source, "account_ids": unique_ids})
    return result


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_int_list(value: Any) -> list[int]:
    if not value:
        return []
    raw_items = [item.strip() for item in value.split(",")] if isinstance(value, str) else list(value) if isinstance(value, (list, tuple, set)) else [value]
    result: list[int] = []
    for item in raw_items:
        number = _as_int(item)
        if number is not None and number not in result:
            result.append(number)
    return result


def _source_ref(source: dict[str, Any]) -> dict[str, str]:
    ref = {
        "source_type": str(source.get("source_type") or "global"),
        "source_id": str(source.get("source_id") or "global"),
    }
    if source.get("keepalive_mode"):
        ref["keepalive_mode"] = str(source["keepalive_mode"])
    return ref


def _copy_probe_meta(meta: dict[str, Any], source: dict[str, Any]) -> None:
    for key in ("session_kind", "session_id", "proxy_id"):
        if source.get(key) is not None and not meta.get(key):
            meta[key] = source.get(key)


def _states_by_account(session: Session, tenant_id: int) -> dict[int, TgAccountOnlineState]:
    rows = session.scalars(select(TgAccountOnlineState).where(TgAccountOnlineState.tenant_id == tenant_id))
    return {row.account_id: row for row in rows}


def _apply_desired_state(state: TgAccountOnlineState, meta: dict[str, Any], now: datetime) -> int:
    before = _state_signature(state)
    state.desired_online = True
    state.desired_sources = meta["sources"]
    state.active_task_count = int(meta.get("active_task_count") or 0)
    state.session_kind = str(meta.get("session_kind") or state.session_kind or "primary")
    state.session_id = str(meta.get("session_id") or state.session_id or "")
    state.proxy_id = meta.get("proxy_id") if meta.get("proxy_id") is not None else state.proxy_id
    state.online_status = _next_desired_status(state)
    if state.online_status != "online" or state.stale_after_at is None:
        state.stale_after_at = stale_deadline_for_state(state, now)
    elif _probe_after_stale_deadline(state):
        state.next_probe_at = now
    state.failure_type = "" if state.online_status != "blocked" else state.failure_type
    state.reconciled_at = now
    state.updated_at = now
    return int(before != _state_signature(state))


def _clear_desired_state(state: TgAccountOnlineState, now: datetime) -> int:
    before = _state_signature(state)
    state.desired_online = False
    state.desired_sources = []
    state.active_task_count = 0
    state.online_status = "offline"
    state.failure_type = "desired_source_removed"
    state.failure_detail = "在线需求来源已移除"
    state.reconciled_at = now
    state.updated_at = now
    return int(before != _state_signature(state))


def _next_desired_status(state: TgAccountOnlineState) -> str:
    if state.online_status == "online":
        return "online"
    if state.online_status in {"blocked", "login_required"}:
        return state.online_status
    return "warming"


def _probe_after_stale_deadline(state: TgAccountOnlineState) -> bool:
    stale_after = as_beijing(state.stale_after_at)
    next_probe = as_beijing(state.next_probe_at)
    return bool(stale_after and (next_probe is None or next_probe >= stale_after))


def _state_is_ready(state: TgAccountOnlineState | None, now: datetime) -> bool:
    if not state or not state.desired_online or state.online_status != "online":
        return False
    return not _state_is_stale(state, now)


def _state_is_available(state: TgAccountOnlineState | None, now: datetime) -> bool:
    if not state or not state.desired_online or state.online_status not in ONLINE_AVAILABLE_STATUSES:
        return False
    return not _state_is_stale(state, now)


def _state_is_stale(state: TgAccountOnlineState, now: datetime) -> bool:
    stale_after = as_beijing(state.stale_after_at)
    current_time = as_beijing(now) or now
    return bool(stale_after and stale_after <= current_time)


def _state_signature(state: TgAccountOnlineState) -> tuple[Any, ...]:
    return (
        state.desired_online,
        state.desired_sources,
        state.online_status,
        state.session_kind,
        state.session_id,
        state.proxy_id,
        state.active_task_count,
    )


__all__ = ["drain_account_online_keepalive", "is_account_online_available", "is_account_online_ready", "is_account_online_ready_for_planning", "mark_stale_online_states", "probe_due_online_states", "reconcile_account_online_sources", "reconcile_runtime_online_sources"]
