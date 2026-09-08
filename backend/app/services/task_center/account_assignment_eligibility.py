"""Current account identity is required before assigning operational work."""
from collections import Counter

from sqlalchemy import String, and_, case, cast, func, or_, select, true
from sqlalchemy.exc import DBAPIError

from app.models import AccountStatus, Action, Task, TgAccount, TgAccountAuthorization, TgAccountOnlineState

from .engagement_runtime_error import RuntimeResourceBlocked

INVALID_SESSION_FAILURES = ("session_invalid", "login_required", "relogin_required")
INVALID_AUTHORIZATION_STATES = ("invalid", "revoked", "expired")
UNIFIED_CONTRACT = "unified_engagement_v1"


def _reason_expression():
    account, authorization, online = TgAccount, TgAccountAuthorization, TgAccountOnlineState
    has_current = account.current_authorization_id.is_not(None)
    valid_current = and_(authorization.id == account.current_authorization_id,
        authorization.tenant_id == account.tenant_id, authorization.account_id == account.id,
        authorization.is_current.is_(True), authorization.is_slot_current.is_(True),
        authorization.status == "active", func.length(authorization.session_ciphertext) > 0)
    return case(
        (account.deleted_at.is_not(None), "account_deleted"),
        (account.telegram_frozen.is_(True), "account_frozen"),
        (account.status.in_((AccountStatus.SESSION_EXPIRED.value, AccountStatus.NEED_RELOGIN.value)), "session_invalid"),
        (account.status != AccountStatus.ACTIVE.value, "account_status_unavailable"),
        (account.account_lifecycle_status != "business_active", "account_not_business_active"),
        (and_(has_current, ~func.coalesce(valid_current, False)), "current_authorization_unavailable"),
        (and_(has_current, or_(authorization.health_status == "invalid",
            authorization.dr_state == "invalid",
            authorization.remote_authorization_state.in_(INVALID_AUTHORIZATION_STATES))), "authorization_invalid"),
        (and_(~has_current, func.coalesce(func.length(account.session_ciphertext), 0) == 0), "session_unavailable"),
        (and_(func.length(online.session_id) > 0, online.session_id != cast(account.id, String)), "account_identity_unproven"),
        (online.failure_type.in_(INVALID_SESSION_FAILURES), "session_invalid"),
        (and_(online.online_status == "blocked", account.telegram_freeze_observed_at.is_(None)),
            "account_freeze_observation_required"),
        else_="",
    )


def _qualification_query():
    return select(TgAccount.id, _reason_expression().label("reason")).outerjoin(
        TgAccountAuthorization, TgAccountAuthorization.id == TgAccount.current_authorization_id,
    ).outerjoin(TgAccountOnlineState, and_(TgAccountOnlineState.tenant_id == TgAccount.tenant_id,
        TgAccountOnlineState.account_id == TgAccount.id))


def assignment_decisions(session, tenant_id, account_ids, *, lock=True) -> dict[int, str]:
    ids = tuple(sorted(set(map(int, account_ids))))
    if not ids:
        return {}
    session.flush()
    if lock:
        _lock_identities(session, tenant_id, ids)
    rows = session.execute(_qualification_query().where(
        TgAccount.tenant_id == tenant_id, TgAccount.id.in_(ids),
    )).all()
    reasons = {int(row.id): str(row.reason) for row in rows}
    return {account_id: reasons.get(account_id, "account_missing") for account_id in ids}


def _lock_identities(session, tenant_id, ids):
    try:
        with session.begin_nested():
            authorizations = session.scalars(select(TgAccount.current_authorization_id).where(
                TgAccount.tenant_id == tenant_id, TgAccount.id.in_(ids),
            ).order_by(TgAccount.id).with_for_update(nowait=True)).all()
            current_ids = sorted({value for value in authorizations if value is not None})
            if current_ids:
                session.execute(select(TgAccountAuthorization.id).where(
                    TgAccountAuthorization.id.in_(current_ids),
                ).order_by(TgAccountAuthorization.id).with_for_update(read=True, nowait=True)).all()
            session.execute(select(TgAccountOnlineState.account_id).where(
                TgAccountOnlineState.tenant_id == tenant_id, TgAccountOnlineState.account_id.in_(ids),
            ).order_by(TgAccountOnlineState.account_id).with_for_update(read=True, nowait=True)).all()
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) != "55P03":
            raise
        raise RuntimeResourceBlocked("account_eligibility_busy", "账号资格更新中，等待当前事务完成") from error


def eligible_assignment_account_ids(session, tenant_id, account_ids) -> tuple[int, ...]:
    decisions = assignment_decisions(session, tenant_id, account_ids)
    return tuple(account_id for account_id in account_ids if not decisions[int(account_id)])


def publish_assignment_summary(task, decisions):
    excluded = {str(account_id): reason for account_id, reason in decisions.items() if reason}
    task.stats = {**dict(task.stats or {}), "account_assignment_eligibility": {
        "candidate_count": len(decisions), "eligible_count": len(decisions) - len(excluded),
        "excluded_count": len(excluded), "excluded_reasons": dict(Counter(excluded.values())),
        "excluded_accounts": excluded,
        "state": "eligible" if len(decisions) > len(excluded) else "no_eligible_accounts",
    }}


def require_assignment_account(session, task, account_id) -> None:
    if (task.type_config or {}).get("engagement_contract_version") != UNIFIED_CONTRACT:
        return
    if account_id is None:
        raise ValueError("account_assignment_missing")
    reason = assignment_decisions(session, task.tenant_id, (account_id,))[int(account_id)]
    if reason:
        raise RuntimeResourceBlocked(reason, "账号无有效业务资格，不分配或调用新工作")


def action_assignment_reason(session, action) -> str:
    task = session.get(Task, action.task_id)
    if task is None or (task.type_config or {}).get("engagement_contract_version") != UNIFIED_CONTRACT:
        return ""
    if action.account_id is None:
        return "account_assignment_missing"
    ids = [int(action.account_id)]
    if action.action_type == "invite_group_account":
        target_id = (action.payload or {}).get("target_account_id")
        if target_id:
            ids.append(int(target_id))
    reasons = assignment_decisions(session, action.tenant_id, ids)
    return next((reasons[identity] for identity in ids if reasons[identity]), "")


def require_action_assignment_account(session, action) -> None:
    reason = action_assignment_reason(session, action)
    if reason:
        raise RuntimeResourceBlocked(reason, "账号无有效业务资格，不生成或派发新工作")


def action_assignment_predicate():
    eligible = _qualification_query().where(TgAccount.id == Action.account_id,
        TgAccount.tenant_id == Action.tenant_id, _reason_expression() == "").correlate(Action).exists()
    return or_(func.coalesce(Task.type_config["engagement_contract_version"].as_string(), "") != UNIFIED_CONTRACT,
        eligible)


def assignment_account_predicate(task, account_column):
    if (task.type_config or {}).get("engagement_contract_version") != UNIFIED_CONTRACT:
        return true()
    qualified = _qualification_query().with_only_columns(TgAccount.id).where(
        TgAccount.tenant_id == task.tenant_id, _reason_expression() == "").correlate(None)
    return account_column.in_(qualified)
