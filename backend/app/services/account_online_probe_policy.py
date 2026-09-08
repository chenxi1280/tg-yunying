"""Daily automatic rechecks for known account failures; local eligibility stays current."""
from datetime import datetime

from sqlalchemy import or_

from app.models import AccountStatus, TgAccount, TgAccountOnlineState
from app.services.account_online_constants import ONLINE_UNAVAILABLE_PROBE_INTERVAL


DAILY_RECHECK_STATUSES = frozenset({
    AccountStatus.SESSION_EXPIRED.value,
    AccountStatus.NEED_RELOGIN.value,
    AccountStatus.BANNED.value,
    AccountStatus.DISABLED.value,
})
DAILY_RECHECK_FAILURES = frozenset({"account_frozen"})
AUTOMATIC_PROBE_EXCLUDED_STATUSES = frozenset({AccountStatus.BANNED.value, AccountStatus.DISABLED.value})


def requires_daily_probe(account: TgAccount | None, state: TgAccountOnlineState) -> bool:
    return bool(
        (account and (account.telegram_frozen or account.status in DAILY_RECHECK_STATUSES))
        or state.online_status == "login_required"
        or state.failure_type in DAILY_RECHECK_FAILURES
    )


def automatic_probe_due_condition(now: datetime):
    daily = or_(
        TgAccount.telegram_frozen.is_(True),
        TgAccount.status.in_(DAILY_RECHECK_STATUSES),
        TgAccountOnlineState.online_status == "login_required",
        TgAccountOnlineState.failure_type.in_(DAILY_RECHECK_FAILURES),
    )
    return or_(
        TgAccount.id.is_(None),
        ~daily,
        TgAccountOnlineState.last_probe_at.is_(None),
        TgAccountOnlineState.last_probe_at <= now - ONLINE_UNAVAILABLE_PROBE_INTERVAL,
    )
