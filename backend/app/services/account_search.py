from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AccountStatus, TgAccount, TgLoginFlow

from ._common import get_account_phone
from .account_authorization_read_model import authorization_summaries_for_accounts


LOGIN_REQUIRED_STATUSES = {
    AccountStatus.PENDING_LOGIN.value,
    AccountStatus.WAITING_CODE.value,
    AccountStatus.WAITING_QR.value,
    AccountStatus.WAITING_2FA.value,
    AccountStatus.NEED_RELOGIN.value,
    AccountStatus.ERROR.value,
}
LOGIN_PROBLEM_SEARCH_TEXT = (
    "登录有问题 没有登录上平台 待登录 等待验证码 等待扫码 等待2FA "
    "需重新登录 异常 Session失效 Session 失效 session完全失效 session 完全失效 "
    "主授权不可用 主授权缺失 登录失败 验证码没收到 登录验证码没收到"
)


def filter_accounts_by_search(session: Session, accounts: list[TgAccount], search: str | None) -> list[TgAccount]:
    needle = (search or "").strip().lower()
    if not needle:
        return accounts
    flows = _latest_login_flows_by_account(session, [account.id for account in accounts])
    summaries = authorization_summaries_for_accounts(session, accounts)
    return [
        account
        for account in accounts
        if _account_matches_search(account, needle, latest_flow=flows.get(account.id), authorization_summary=summaries.get(account.id))
    ]


def _account_matches_search(
    account: TgAccount,
    needle: str,
    *,
    latest_flow: TgLoginFlow | None = None,
    authorization_summary: dict[str, Any] | None = None,
) -> bool:
    values = _account_search_values(account, latest_flow, authorization_summary)
    return any(needle in str(value).lower() for value in values if value)


def _account_search_values(
    account: TgAccount,
    latest_flow: TgLoginFlow | None,
    authorization_summary: dict[str, Any] | None,
) -> list[Any]:
    values = [
        account.display_name,
        account.username,
        account.phone_masked,
        get_account_phone(account),
        account.status,
        account.profile_sync_status,
        account.profile_sync_error,
        account.developer_app_name,
        account.developer_app_health_status,
        account.proxy_name,
        account.proxy_status,
        account.proxy_alert_status,
    ]
    values.extend(_latest_login_search_values(latest_flow))
    values.extend(_authorization_search_values(account, authorization_summary))
    return values


def _latest_login_search_values(latest_flow: TgLoginFlow | None) -> list[Any]:
    if latest_flow is None:
        return []
    values = [
        latest_flow.method,
        latest_flow.status,
        latest_flow.failure_type,
        latest_flow.failure_detail,
        latest_flow.trace_id,
    ]
    if latest_flow.failure_type or latest_flow.failure_detail:
        values.append("登录失败")
    return values


def _authorization_search_values(account: TgAccount, summary: dict[str, Any] | None) -> list[Any]:
    summary = summary or {}
    values: list[Any] = [summary.get("primary_status"), summary.get("primary_source"), summary.get("risk_hint")]
    if _has_login_issue(account, summary):
        values.append(LOGIN_PROBLEM_SEARCH_TEXT)
    return values


def _has_login_issue(account: TgAccount, authorization_summary: dict[str, Any]) -> bool:
    primary_status = str(authorization_summary.get("primary_status") or "")
    if account.status in LOGIN_REQUIRED_STATUSES:
        return True
    return account.status == AccountStatus.SESSION_EXPIRED.value or primary_status != "active"


def _latest_login_flows_by_account(session: Session, account_ids: list[int]) -> dict[int, TgLoginFlow]:
    if not account_ids:
        return {}
    latest_ids = (
        select(func.max(TgLoginFlow.id).label("id"))
        .where(TgLoginFlow.account_id.in_(account_ids))
        .group_by(TgLoginFlow.account_id)
        .subquery()
    )
    rows = session.scalars(select(TgLoginFlow).join(latest_ids, TgLoginFlow.id == latest_ids.c.id))
    return {row.account_id: row for row in rows}
