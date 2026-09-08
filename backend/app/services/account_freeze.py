"""Persist account-level freeze observations without settling any Action."""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import object_session
from sqlalchemy.orm.attributes import set_committed_value

from app.integrations.telegram.account_freeze import FROZEN_HEALTH_SCORE
from app.models import AccountStatus, TgAccount, TgAccountOnlineState

class AccountFrozenBeforeGateway(ValueError):
    pass


def lock_account_freeze_state(session, account: TgAccount) -> None:
    row = session.execute(select(
        TgAccount.status, TgAccount.telegram_frozen, TgAccount.telegram_freeze_observed_at,
        TgAccount.authorization_generation, TgAccount.connection_generation,
        TgAccount.deleted_at, TgAccount.health_score,
    ).where(TgAccount.id == account.id, TgAccount.tenant_id == account.tenant_id).with_for_update()).one()
    for name, value in row._mapping.items():
        set_committed_value(account, name, value)


def apply_freeze_observation(account: TgAccount, *, frozen: bool, observed_at: datetime) -> bool:
    session = object_session(account)
    if session is not None:
        lock_account_freeze_state(session, account)
    previous = account.telegram_freeze_observed_at
    if previous is not None and _aware(previous) >= _aware(observed_at):
        return False
    account.telegram_frozen = frozen
    account.telegram_freeze_observed_at = observed_at
    if frozen and account.status not in {AccountStatus.BANNED.value, AccountStatus.DISABLED.value}:
        account.status = AccountStatus.SUSPECTED_BANNED.value
        account.health_score = min(account.health_score, FROZEN_HEALTH_SCORE)
    if session is not None:
        session.flush([account])
    return True


def mark_account_frozen(account: TgAccount) -> None:
    apply_freeze_observation(account, frozen=True, observed_at=datetime.now(timezone.utc))


def guard_account_call_start(session, attempt) -> None:
    from app.services.task_center.engagement_runtime_error import RuntimeResourceBlocked

    try:
        with session.begin_nested():
            row = session.execute(select(TgAccount.telegram_frozen, TgAccount.telegram_freeze_observed_at).where(
                TgAccount.id == attempt.account_id, TgAccount.tenant_id == attempt.tenant_id,
            ).with_for_update(read=True, nowait=True)).one()
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != "55P03":
            raise
        raise RuntimeResourceBlocked("account_freeze_admission_busy", "账号资格观测正在更新") from exc
    if not row.telegram_frozen:
        _guard_unobserved_blocked_account(session, attempt, row.telegram_freeze_observed_at)
        return
    if attempt.gateway_call_started_at is not None:
        raise RuntimeError("account_freeze_guard_attempt_already_called")
    attempt.status = "skipped_before_gateway"
    attempt.after_call_at = datetime.now(timezone.utc)
    attempt.failure_type = "account_frozen"
    attempt.result_snapshot = {**dict(attempt.result_snapshot or {}), "remote_mutation_started": False}
    raise AccountFrozenBeforeGateway("account_frozen: Telegram 已冻结此账号，本次调用未发出")


def _guard_unobserved_blocked_account(session, attempt, observed_at) -> None:
    from app.services.task_center.engagement_runtime_error import RuntimeResourceBlocked

    if observed_at is not None:
        return
    online_status = session.scalar(select(TgAccountOnlineState.online_status).where(
        TgAccountOnlineState.account_id == attempt.account_id,
        TgAccountOnlineState.tenant_id == attempt.tenant_id,
    ))
    if online_status == "blocked":
        raise RuntimeResourceBlocked(
            "account_freeze_observation_required", "账号健康探测失败，尚未取得Telegram冻结状态观测",
        )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
