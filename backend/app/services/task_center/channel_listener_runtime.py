from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import hashlib
import logging
import os
import socket

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ChannelCommentPlanContract,
    ChannelMessage,
    ListenerSourceState,
    OperationTarget,
    Task,
    TaskSourceSubscription,
    TgAccount,
)
from app.services._common import _now, gateway
from app.services.channel_target_reference import channel_read_reference
from app.services.account_runtime_transport import task_account_runtime_transport

from .account_pool import select_task_accounts
from .channel_listener_reactions import credential_task
from .channel_listener_reactions import probe_reaction_capability
from .channel_listener_reactions import record_reaction_probe_state
from .channel_listener_snapshot_persistence import persist_channel_snapshot
from .channel_source_pagination import source_page_offset
from .channel_source_history import initial_history_ready
from .channel_listener_claim import ChannelSourceClaimLost, locked_source_state
from .channel_comment_listener_errors import (
    clear_owned_listener_errors,
    record_listener_error,
)
from .planner_wake import wake_task_planner


CHANNEL_TASK_TYPES = frozenset({"channel_comment", "channel_like", "channel_view"})
CHANNEL_TASK_STATUSES = frozenset({"pending", "running"})


@dataclass
class ChannelListenerSource:
    tenant_id: int
    channel_target_id: int
    source_peer_hash: str
    account_id: int
    collect_window_seconds: int
    fetch_limit: int
    reaction_capability_required: bool = False
    task_ids: list[str] = field(default_factory=list)
    fetch_offset_id: int = 0
    claim_owner: str = ""
    claimed_revision: int | None = None


@dataclass(frozen=True)
class ChannelListenerDrainResult:
    source_count: int = 0
    processed_count: int = 0
    error_count: int = 0


def drain_channel_listener_runtime(
    session_factory,
    *,
    tenant_id: int | None = None,
    limit: int = 50,
) -> ChannelListenerDrainResult:
    with session_factory() as session:
        sources = _channel_sources(session, tenant_id=tenant_id, limit=limit)
        session.commit()
    processed = 0
    errors = 0
    for source in sources:
        outcome = _drain_channel_source(session_factory, source)
        processed += int(outcome == "processed")
        errors += int(outcome == "error")
    return ChannelListenerDrainResult(len(sources), processed, errors)


def ensure_channel_subscription(
    session: Session,
    task: Task,
    channel: OperationTarget,
) -> TaskSourceSubscription:
    source_hash = _source_hash(channel.tg_peer_id)
    subscription = session.scalar(select(TaskSourceSubscription).where(
        TaskSourceSubscription.tenant_id == task.tenant_id,
        TaskSourceSubscription.task_id == task.id,
        TaskSourceSubscription.lifecycle_epoch == int(task.task_lifecycle_epoch or 1),
        TaskSourceSubscription.source_type == "channel",
        TaskSourceSubscription.source_peer_hash == source_hash,
    ))
    if subscription is None:
        subscription = TaskSourceSubscription(
            tenant_id=task.tenant_id,
            task_id=task.id,
            lifecycle_epoch=int(task.task_lifecycle_epoch or 1),
            source_type="channel",
            source_peer_hash=source_hash,
            target_reference_revision=channel.reference_revision,
            listener_revision=0,
        )
        session.add(subscription)
        session.flush()
    return subscription


def channel_snapshot_state(
    session: Session,
    task: Task,
    channel: OperationTarget,
    *,
    now_value: datetime | None = None,
) -> tuple[str, datetime | None]:
    status, next_probe_at, _state_id, _revision = channel_snapshot_binding(
        session,
        task,
        channel,
        now_value=now_value,
    )
    return status, next_probe_at


def channel_snapshot_binding(
    session: Session,
    task: Task,
    channel: OperationTarget,
    *,
    now_value: datetime | None = None,
) -> tuple[str, datetime | None, str | None, int]:
    subscription = ensure_channel_subscription(session, task, channel)
    state = (
        session.get(ListenerSourceState, subscription.listener_source_state_id)
        if subscription.listener_source_state_id
        else None
    )
    if state is None:
        if subscription.state in {"error", "unavailable"}:
            return subscription.state, None, None, 0
        return "pending", None, None, 0
    timestamp = _wall(now_value or _now())
    if state.snapshot_status == "collecting":
        if state.lease_expires_at is None or _wall(state.lease_expires_at) < timestamp:
            return "stale", state.next_probe_at, state.id, int(state.snapshot_revision or 0)
    if state.snapshot_status != "ready":
        return state.snapshot_status, state.next_probe_at, state.id, int(state.snapshot_revision or 0)
    if state.fresh_until_at is None or _wall(state.fresh_until_at) < timestamp:
        return "stale", state.next_probe_at, state.id, int(state.snapshot_revision or 0)
    if int(state.snapshot_revision or 0) < int(subscription.required_snapshot_revision or 0):
        return "pending", state.next_probe_at, state.id, int(state.snapshot_revision or 0)
    if not initial_history_ready(session, task, state=state):
        return "pending", state.next_probe_at, state.id, int(state.snapshot_revision or 0)
    return "ready", state.next_probe_at, state.id, int(state.snapshot_revision or 0)


def request_channel_snapshot_refresh(session: Session, task: Task) -> None:
    channel = _channel_for_task(session, task)
    if channel is None:
        return
    subscription = ensure_channel_subscription(session, task, channel)
    state = (
        session.get(ListenerSourceState, subscription.listener_source_state_id)
        if subscription.listener_source_state_id
        else None
    )
    if state is not None:
        subscription.required_snapshot_revision = max(
            int(subscription.required_snapshot_revision or 0),
            int(state.snapshot_revision or 0) + 1,
        )
        state.next_probe_at = _now()
    subscription.state = "pending"


def _channel_sources(
    session: Session,
    *,
    tenant_id: int | None,
    limit: int,
) -> list[ChannelListenerSource]:
    conditions = [
        Task.type.in_(CHANNEL_TASK_TYPES),
        Task.status.in_(CHANNEL_TASK_STATUSES),
        Task.deleted_at.is_(None),
    ]
    if tenant_id is not None:
        conditions.append(Task.tenant_id == tenant_id)
    tasks = session.scalars(
        select(Task).where(*conditions).order_by(Task.priority, Task.created_at).limit(limit * 3)
    )
    sources: dict[tuple[int, int], ChannelListenerSource] = {}
    for task in tasks:
        channel = _channel_for_task(session, task)
        if channel is None:
            continue
        source = _source_for_task(session, task, channel)
        if source is None:
            _mark_subscription_unavailable(session, task, channel)
            continue
        key = (source.tenant_id, source.channel_target_id)
        current = sources.setdefault(key, source)
        current.reaction_capability_required = (
            current.reaction_capability_required
            or source.reaction_capability_required
        )
        if task.id not in current.task_ids:
            current.task_ids.append(task.id)
        _bind_subscription(session, task, current)
        if len(sources) >= max(1, limit):
            break
    return list(sources.values())


def _channel_for_task(session: Session, task: Task) -> OperationTarget | None:
    config = task.type_config if isinstance(task.type_config, dict) else {}
    target_id = int(config.get("target_channel_id") or config.get("target_operation_target_id") or 0)
    channel = session.get(OperationTarget, target_id) if target_id else None
    if channel is None or channel.tenant_id != task.tenant_id or channel.target_type != "channel":
        return None
    return channel


def _source_for_task(
    session: Session,
    task: Task,
    channel: OperationTarget,
) -> ChannelListenerSource | None:
    config = task.type_config if isinstance(task.type_config, dict) else {}
    accounts = select_task_accounts(
        session,
        task.tenant_id,
        task.account_config or {},
        limit=1,
        enforce_capacity=False,
    )
    if not accounts:
        return None
    window = max(1, int(config.get("listener_interval_seconds") or 30))
    return ChannelListenerSource(
        tenant_id=task.tenant_id,
        channel_target_id=channel.id,
        source_peer_hash=_source_hash(channel.tg_peer_id),
        account_id=accounts[0].id,
        collect_window_seconds=window,
        fetch_limit=_fetch_limit(config),
        reaction_capability_required=task.type == "channel_like",
        task_ids=[task.id],
    )


def _mark_subscription_unavailable(
    session: Session,
    task: Task,
    channel: OperationTarget,
) -> None:
    subscription = ensure_channel_subscription(session, task, channel)
    changed = subscription.state != "unavailable" or subscription.listener_source_state_id is not None
    subscription.listener_source_state_id = None
    subscription.state = "unavailable"
    task.last_error = "channel_source_snapshot_unavailable"
    if changed:
        wake_task_planner(
            session,
            task,
            reason_code="channel_source_snapshot_unavailable",
            not_before_at=_now(),
        )


def _bind_subscription(
    session: Session,
    task: Task,
    source: ChannelListenerSource,
) -> None:
    channel = session.get(OperationTarget, source.channel_target_id)
    if channel is None:
        return
    subscription = ensure_channel_subscription(session, task, channel)
    state = _listener_state(session, source)
    subscription.listener_source_state_id = state.id
    subscription.state = "active"


def _listener_state(
    session: Session,
    source: ChannelListenerSource,
) -> ListenerSourceState:
    state = session.scalar(select(ListenerSourceState).where(
        ListenerSourceState.tenant_id == source.tenant_id,
        ListenerSourceState.source_type == "channel",
        ListenerSourceState.source_peer_id == str(source.channel_target_id),
        ListenerSourceState.account_id == source.account_id,
    ))
    if state is None:
        state = ListenerSourceState(
            tenant_id=source.tenant_id,
            source_type="channel",
            source_peer_id=str(source.channel_target_id),
            account_id=source.account_id,
            shard_key=f"channel:{source.channel_target_id}",
            collect_window_seconds=source.collect_window_seconds,
        )
        session.add(state)
        session.flush()
    return state


def _drain_channel_source(session_factory, source: ChannelListenerSource) -> str:
    with session_factory() as session:
        state = _claim_source(session, source)
        if state is None:
            return "skipped"
        source = replace(source, fetch_offset_id=source_page_offset(session, state.id),
            claim_owner=state.lease_owner, claimed_revision=state.snapshot_revision)
        fetch = _fetch_context(session, source)
        if fetch is None:
            _mark_error(
                session,
                source,
                state,
                code="channel_listener_account_unavailable",
            )
            session.commit()
            return "error"
        tracked_message_ids = _tracked_message_ids(session, source)
        session.commit()
        try:
            _collect_and_persist_source_page(session, source, state_id=state.id,
                fetch=fetch, tracked_message_ids=tracked_message_ids)
            session.commit()
        except ChannelSourceClaimLost:
            session.rollback()
            return "skipped"
        except Exception as exc:  # noqa: BLE001 - typed state remains visible to Planner.
            logging.getLogger(__name__).exception("Channel source page failed state_id=%s", state.id)
            session.rollback()
            try:
                state = locked_source_state(session, source, state.id)
            except ChannelSourceClaimLost:
                return "skipped"
            _mark_error(session, source, state, code=type(exc).__name__)
            session.commit()
            return "error"
        return "processed"


def _collect_and_persist_source_page(session, source, *, state_id, fetch, tracked_message_ids):
    channel_peer, session_ciphertext, credentials = fetch
    observations = _fetch_source_observations(source, channel_peer=channel_peer,
        session_ciphertext=session_ciphertext, credentials=credentials, tracked_message_ids=tracked_message_ids)
    _persist_source_result(session, source, state_id=state_id, snapshots=observations[0],
        channel_peer=channel_peer, session_ciphertext=session_ciphertext, credentials=credentials,
        deletion_observations=observations[1], discussion_snapshot=observations[2],
        discussion_probe_error=observations[3], source_observed_at=observations[4])


def _fetch_source_observations(
    source: ChannelListenerSource,
    *,
    channel_peer,
    session_ciphertext,
    credentials,
    tracked_message_ids: list[int],
) -> tuple:
    page = {"offset_id": source.fetch_offset_id} if source.fetch_offset_id else {}
    snapshots = gateway.fetch_channel_messages(
        source.account_id,
        channel_peer,
        session_ciphertext=session_ciphertext,
        credentials=credentials,
        limit=source.fetch_limit,
        **page,
    )
    source_observed_at = _now()
    discussion_snapshot, discussion_probe_error = _probe_channel_discussion(
        source, snapshots=snapshots, channel_peer=channel_peer,
        session_ciphertext=session_ciphertext, credentials=credentials,
    )
    deletions = _probe_missing_messages(
        source, snapshots=snapshots, tracked_message_ids=tracked_message_ids,
        channel_peer=channel_peer, session_ciphertext=session_ciphertext,
        credentials=credentials,
    )
    return snapshots, deletions, discussion_snapshot, discussion_probe_error, source_observed_at


def _tracked_message_ids(
    session: Session,
    source: ChannelListenerSource,
) -> list[int]:
    return list(session.scalars(
        select(ChannelMessage.message_id)
        .join(
            ChannelCommentPlanContract,
            ChannelCommentPlanContract.channel_message_id == ChannelMessage.id,
        )
        .where(
            ChannelMessage.tenant_id == source.tenant_id,
            ChannelMessage.channel_target_id == source.channel_target_id,
            ChannelCommentPlanContract.contract_state == "open",
        )
        .distinct()
        .order_by(ChannelMessage.message_id)
    ))


def _probe_missing_messages(
    source: ChannelListenerSource,
    *,
    snapshots,
    tracked_message_ids: list[int],
    channel_peer,
    session_ciphertext,
    credentials,
):
    present = {int(snapshot.message_id) for snapshot in snapshots}
    missing = [message_id for message_id in tracked_message_ids if message_id not in present]
    if not missing:
        return []
    return gateway.fetch_channel_message_deletions(
        source.account_id, channel_peer, missing,
        session_ciphertext=session_ciphertext,
        credentials=credentials,
    )


def _probe_channel_discussion(
    source: ChannelListenerSource,
    *,
    snapshots,
    channel_peer,
    session_ciphertext,
    credentials,
):
    try:
        result = gateway.fetch_channel_discussion_identity(
            source.account_id,
            channel_peer,
            source_message_ids=[int(item.message_id) for item in snapshots],
            session_ciphertext=session_ciphertext,
            credentials=credentials,
        )
        return result, ""
    except Exception as exc:  # noqa: BLE001 - probe failure is persisted, not inferred as unbound.
        return None, type(exc).__name__


def _persist_source_result(
    session: Session,
    source: ChannelListenerSource,
    *,
    state_id: str,
    snapshots,
    channel_peer,
    session_ciphertext,
    credentials,
    deletion_observations,
    discussion_snapshot,
    discussion_probe_error: str,
    source_observed_at: datetime | None = None,
) -> None:
    reaction_capability, probe_error = probe_reaction_capability(
        gateway.fetch_channel_reaction_capability,
        required=source.reaction_capability_required,
        account_id=source.account_id,
        channel_peer=channel_peer,
        session_ciphertext=session_ciphertext,
        credentials=credentials,
    )
    persist_channel_snapshot(
        session,
        source,
        state_id=state_id,
        snapshots=snapshots,
        deletion_observations=deletion_observations,
        discussion_snapshot=discussion_snapshot,
        discussion_probe_error=discussion_probe_error,
        reaction_capability=reaction_capability,
        now_value=source_observed_at or _now(),
        wake_subscribers=_wake_subscribers,
    )
    record_reaction_probe_state(
        session,
        task_ids=source.task_ids,
        required=source.reaction_capability_required,
        error_code=probe_error,
    )


def _claim_source(
    session: Session,
    source: ChannelListenerSource,
) -> ListenerSourceState | None:
    state = _listener_state(session, source)
    state = session.scalar(select(ListenerSourceState).where(ListenerSourceState.id == state.id)
        .with_for_update(skip_locked=True).execution_options(populate_existing=True))
    if state is None:
        return None
    now_value = _now()
    if state.next_probe_at and _wall(state.next_probe_at) > _wall(now_value):
        return None
    owner = _listener_owner()
    if (
        state.lease_owner
        and state.lease_expires_at
        and _wall(state.lease_expires_at) > _wall(now_value)
    ):
        return None
    state.lease_owner = owner
    state.lease_expires_at = now_value + timedelta(seconds=source.collect_window_seconds * 2)
    state.snapshot_status = "collecting"
    session.commit()
    return state


def _fetch_context(session: Session, source: ChannelListenerSource):
    channel = session.get(OperationTarget, source.channel_target_id)
    account = session.get(TgAccount, source.account_id)
    task = credential_task(
        session,
        task_ids=source.task_ids,
        reaction_capability_required=source.reaction_capability_required,
    )
    if channel is None or account is None or task is None:
        return None
    try:
        transport = task_account_runtime_transport(session, account, task.type)
    except ValueError:
        return None
    return (
        channel_read_reference(channel),
        transport.session_ciphertext,
        transport.credentials,
    )


def _mark_error(
    session: Session,
    source: ChannelListenerSource,
    state: ListenerSourceState,
    *,
    code: str,
) -> None:
    now_value = _now()
    state.snapshot_status = "unavailable"
    state.last_error_code = code[:80]
    state.last_error = code[:500]
    state.fresh_until_at = None
    state.next_probe_at = now_value + timedelta(seconds=source.collect_window_seconds)
    state.lease_owner = ""
    state.lease_expires_at = None
    _wake_subscribers(
        session,
        source,
        state,
        reason="channel_source_snapshot_unavailable",
    )


def _wake_subscribers(session, source, state, *, reason: str) -> None:
    channel = session.get(OperationTarget, source.channel_target_id)
    for task_id in source.task_ids:
        task = session.get(Task, task_id)
        if task is None:
            continue
        subscription = session.scalar(select(TaskSourceSubscription).where(
            TaskSourceSubscription.task_id == task.id,
            TaskSourceSubscription.lifecycle_epoch == int(task.task_lifecycle_epoch or 1),
            TaskSourceSubscription.source_peer_hash == source.source_peer_hash,
        ))
        if subscription:
            subscription.listener_source_state_id = state.id
            subscription.required_snapshot_revision = int(state.snapshot_revision or 0)
            subscription.target_reference_revision = int(channel.reference_revision if channel else 0)
            subscription.listener_revision = int(state.snapshot_revision or 0) + int(state.snapshot_status != "ready")
            subscription.state = state.snapshot_status
            _project_listener_error(
                session, task, subscription=subscription, state=state,
            )
        wake_task_planner(session, task, reason_code=reason, not_before_at=_now())


def _project_listener_error(session, task, *, subscription, state) -> None:
    observed_at = state.observed_at or _now()
    if state.snapshot_status == "ready":
        clear_owned_listener_errors(
            session, task, subscription, cleared_at=observed_at,
        )
        return
    record_listener_error(
        session, task, subscription,
        error_code=state.last_error_code or "channel_source_snapshot_unavailable",
        detail=state.last_error,
        observed_at=observed_at,
    )


def _fetch_limit(config: dict) -> int:
    scope = str(config.get("message_scope") or config.get("scope") or "").strip()
    if scope in {"latest_n", "dynamic_new"}:
        return max(1, min(100, int(config.get("latest_message_count") or 10)))
    return 50


def _source_hash(peer: str) -> str:
    return hashlib.sha256(str(peer).strip().lower().encode("utf-8")).hexdigest()


def _listener_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:channel-listener"


def _wall(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value


__all__ = [
    "ChannelListenerDrainResult",
    "channel_snapshot_binding",
    "channel_snapshot_state",
    "drain_channel_listener_runtime",
    "ensure_channel_subscription",
    "request_channel_snapshot_refresh",
]
