from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AccountRuntimeSummary, AccountStatus, TgAccount, TgLoginFlow

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
STANDBY_GAP_SEARCH_TEXT = "备用 session 缺口 健康备用 session 不足 2 个 备用 session 未登录"
AUTHORIZATION_RESCUE_SEARCH_TEXT = "可从备用 session 激活恢复 主授权掉线互救 三槽位自愈"


def filter_accounts_by_search(session: Session, accounts: list[TgAccount], search: str | None) -> list[TgAccount]:
    needle = (search or "").strip().lower()
    if not needle:
        return accounts
    flows = _latest_login_flows_by_account(session, [account.id for account in accounts])
    summaries = authorization_summaries_for_accounts(session, accounts)
    runtime_summaries = _runtime_summaries_by_account(session, [account.id for account in accounts])
    return [
        account
        for account in accounts
        if _account_matches_search(
            account,
            needle,
            latest_flow=flows.get(account.id),
            authorization_summary=summaries.get(account.id),
            runtime_summary=runtime_summaries.get(account.id),
        )
    ]


def _account_matches_search(
    account: TgAccount,
    needle: str,
    *,
    latest_flow: TgLoginFlow | None = None,
    authorization_summary: dict[str, Any] | None = None,
    runtime_summary: AccountRuntimeSummary | None = None,
) -> bool:
    values = _account_search_values(account, latest_flow, authorization_summary, runtime_summary)
    return any(needle in str(value).lower() for value in values if value)


def _account_search_values(
    account: TgAccount,
    latest_flow: TgLoginFlow | None,
    authorization_summary: dict[str, Any] | None,
    runtime_summary: AccountRuntimeSummary | None,
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
    values.extend(_runtime_summary_search_values(runtime_summary))
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
    slot_statuses = summary.get("slot_statuses") if isinstance(summary.get("slot_statuses"), dict) else {}
    values.extend(_slot_search_values(slot_statuses))
    values.append(summary.get("aggregate_status"))
    if int(summary.get("standby_count") or 0) < int(summary.get("target_standby_count") or 2):
        values.append(STANDBY_GAP_SEARCH_TEXT)
    if summary.get("can_rescue"):
        values.append(AUTHORIZATION_RESCUE_SEARCH_TEXT)
    if _has_login_issue(account, summary):
        values.append(LOGIN_PROBLEM_SEARCH_TEXT)
    return values


def _runtime_summary_search_values(summary: AccountRuntimeSummary | None) -> list[Any]:
    if summary is None:
        return []
    values: list[Any] = [
        summary.unavailable_reason,
        f"容量 {int(summary.remaining_capacity or 0)}/100",
        f"剩余容量 {int(summary.remaining_capacity or 0)}",
    ]
    trend = summary.failure_trend if isinstance(summary.failure_trend, dict) else {}
    values.extend(_runtime_trend_search_values(trend))
    return values


def _runtime_trend_search_values(trend: dict[str, Any]) -> list[Any]:
    values: list[Any] = list(trend.values())
    external_count = int(trend.get("external_authorization_count") or 0)
    if external_count > 0:
        values.append(f"非平台设备 {external_count}")
        values.append("安全待刷新")
    return values


def _slot_search_values(slot_statuses: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for role, status in slot_statuses.items():
        values.append(f"{role} {status}")
        if role in {"standby_1", "standby_2"} and status in {"missing", "down", "manual_required"}:
            values.append(f"{role} session 缺失")
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


def _runtime_summaries_by_account(session: Session, account_ids: list[int]) -> dict[int, AccountRuntimeSummary]:
    if not account_ids:
        return {}
    rows = session.scalars(select(AccountRuntimeSummary).where(AccountRuntimeSummary.account_id.in_(account_ids)))
    return {row.account_id: row for row in rows}
