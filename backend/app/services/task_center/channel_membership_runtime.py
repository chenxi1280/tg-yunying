"""Serialize membership admission against durable Gateway attempts, across tasks."""
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from app.models import Action, ExecutionAttempt, OperationTarget

from .channel_membership_schedule import MIN_MEMBERSHIP_GAP_SECONDS
from .source_pacing import wall_datetime


CHANNEL_MEMBERSHIP_MAX_CONCURRENT = 2
ACCOUNT_MEMBERSHIP_COOLDOWN_SECONDS = 60
MEMBERSHIP_ACTION_TYPES = ("ensure_channel_membership", "ensure_target_membership")
UNKNOWN_ATTEMPT_STATES = ("result_unknown", "unknown_after_send", "remote_unknown")


@dataclass(frozen=True)
class MembershipRuntimeWait:
    retry_at: datetime
    code: str
    detail: str


def membership_runtime_wait(
    session: Session,
    action: Action,
    *,
    target: OperationTarget,
    now: datetime,
) -> MembershipRuntimeWait | None:
    """Caller commits the admitted Gateway attempt before releasing these locks."""
    if target.target_type != "channel":
        return None
    peer = str(target.tg_peer_id or "").strip()
    if not peer or target.tenant_id != action.tenant_id:
        raise ValueError("membership_runtime_target_identity_missing")
    _lock_scopes(session, action, peer=peer)
    account_wait = _account_wait(session, action, now=now)
    if account_wait is not None:
        return account_wait
    peer_targets = select(OperationTarget.id).where(
        OperationTarget.tenant_id == action.tenant_id,
        OperationTarget.target_type == "channel",
        OperationTarget.tg_peer_id == peer,
    )
    running = session.scalar(_membership_attempts(action.tenant_id).where(
        Action.payload["channel_target_id"].as_integer().in_(peer_targets),
        _unsettled_attempt(),
    ).with_only_columns(func.count()))
    if running >= CHANNEL_MEMBERSHIP_MAX_CONCURRENT:
        return MembershipRuntimeWait(
            now + timedelta(seconds=MIN_MEMBERSHIP_GAP_SECONDS),
            "channel_membership_concurrency_wait",
            "同频道已有两个未结束的关注调用，等待名额释放",
        )
    return None


def _lock_scopes(session: Session, action: Action, *, peer: str) -> None:
    if session.get_bind().dialect.name == "sqlite":
        return  # SQLite is used only by single-writer unit tests.
    if session.get_bind().dialect.name != "postgresql":
        raise RuntimeError("membership concurrency requires PostgreSQL")
    scopes = (
        f"membership-account:{action.tenant_id}:{action.account_id}",
        f"membership-channel:{action.tenant_id}:{peer}",
    )
    keys = sorted(int.from_bytes(hashlib.sha256(s.encode()).digest()[:8], "big", signed=True) for s in scopes)
    for key in keys:
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def _membership_attempts(tenant_id: int):
    return select(ExecutionAttempt).join(Action, Action.id == ExecutionAttempt.action_id).where(
        ExecutionAttempt.tenant_id == tenant_id,
        Action.tenant_id == tenant_id,
        Action.action_type.in_(MEMBERSHIP_ACTION_TYPES),
        ExecutionAttempt.gateway_call_started_at.is_not(None),
    )


def _unsettled_attempt():
    return or_(
        ExecutionAttempt.after_call_at.is_(None),
        ExecutionAttempt.status.in_(UNKNOWN_ATTEMPT_STATES),
    )


def _account_wait(session: Session, action: Action, *, now: datetime) -> MembershipRuntimeWait | None:
    attempts = _membership_attempts(action.tenant_id).where(
        ExecutionAttempt.account_id == action.account_id,
    )
    active = session.scalar(attempts.where(
        _unsettled_attempt(),
    ).with_only_columns(ExecutionAttempt.id).limit(1))
    if active:
        return MembershipRuntimeWait(
            now + timedelta(seconds=MIN_MEMBERSHIP_GAP_SECONDS),
            "account_membership_inflight_wait",
            "账号已有未结束的准入调用",
        )
    completed = session.scalar(attempts.where(
        ExecutionAttempt.status == "success",
    ).with_only_columns(func.max(ExecutionAttempt.after_call_at)))
    if completed is None:
        return None
    retry_at = wall_datetime(completed) + timedelta(seconds=ACCOUNT_MEMBERSHIP_COOLDOWN_SECONDS)
    if retry_at <= now:
        return None
    return MembershipRuntimeWait(retry_at, "account_membership_cooldown", "账号关注成功后冷却 60 秒")
