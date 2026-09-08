"""Serialize health projections with account freeze observations."""
from app.integrations.telegram.account_freeze import FROZEN_DETAIL, is_account_frozen_error
from app.models import AccountStatus
from app.services.account_freeze import apply_freeze_observation, lock_account_freeze_state
from app.services.account_online_constants import ONLINE_PROBE_FAILURE_RETRY_AFTER


def guard_probe_freeze_result(session, account, *, state, result, now) -> bool:
    lock_account_freeze_state(session, account)
    current_generations = (account.authorization_generation, account.connection_generation)
    if result.generations is not None and result.generations != current_generations:
        _block(state, now, code="account_health_probe_stale_identity", detail="健康检查期间账号授权或连接代次已改变")
        return True
    if account.deleted_at is not None or account.status in {AccountStatus.BANNED.value, AccountStatus.DISABLED.value}:
        _block(state, now, code="account_unavailable", detail="账号已删除、禁用或封禁")
        return True
    if result.error is not None and is_account_frozen_error(result.error.__class__.__name__, result.error):
        apply_freeze_observation(account, frozen=True, observed_at=result.completed_at or now)
    if result.health is not None and result.health.telegram_frozen is not None:
        health = result.health
        if health.freeze_observed_at is None:
            raise ValueError("telegram_freeze_observation_time_required")
        if not apply_freeze_observation(account, frozen=health.telegram_frozen, observed_at=health.freeze_observed_at):
            _block(state, now, code="account_health_probe_stale_observation", detail="健康检查早于当前冻结状态观测")
            return True
    if account.telegram_frozen:
        account.status = AccountStatus.SUSPECTED_BANNED.value
        _block(state, now, code="account_frozen", detail=FROZEN_DETAIL)
        return True
    return False


def _block(state, now, *, code, detail) -> None:
    state.online_status = "blocked"
    state.failure_type = code
    state.failure_detail = detail
    state.last_probe_at = now
    state.next_probe_at = now + ONLINE_PROBE_FAILURE_RETRY_AFTER
    state.updated_at = now
