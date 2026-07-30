from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AccountStatus,
    GroupBotAdmission,
    TaskAccountDailyCoverage,
    TaskMembershipAdmissionItem,
    TgAccount,
    TgGroup,
    TgGroupAccount,
)
from app.security import decrypt_session

from .group_bot_admission import plannable_admission_account_ids


def refresh_rows(
    session: Session,
    rows_and_items: list[tuple[TaskAccountDailyCoverage, TaskMembershipAdmissionItem]],
    group: TgGroup,
    timestamp: datetime,
) -> None:
    refreshable = [pair for pair in rows_and_items if _row_needs_refresh(pair[0], timestamp)]
    readiness = _account_readiness_batch(
        session,
        [row.account_id for row, _item in refreshable],
        group,
    )
    for row, item in refreshable:
        state, code, detail = readiness[row.account_id]
        _apply_row_readiness(row, item, state, code, detail, timestamp)


def _row_needs_refresh(row: TaskAccountDailyCoverage, timestamp: datetime) -> bool:
    if row.state in {"confirmed", "reserved", "sending", "unknown"}:
        return False
    if row.blocker_stage == "generation_contract":
        return False
    return not (
        row.state == "blocked"
        and row.next_eligible_at
        and _wall_time(row.next_eligible_at) > _wall_time(timestamp)
    )


def _wall_time(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value


def _apply_row_readiness(
    row: TaskAccountDailyCoverage,
    item: TaskMembershipAdmissionItem,
    state: str,
    code: str,
    detail: str,
    timestamp: datetime,
) -> None:
    was_ready = row.state == "ready"
    row.state = state
    row.blocker_code = code
    row.blocker_detail = detail
    row.next_eligible_at = None
    row.membership_item_id = item.id
    if state == "ready" and not was_ready:
        row.targeted_at = timestamp
    row.updated_at = timestamp
    _sync_item_phase(item, state, code, detail)


def _account_readiness_batch(
    session: Session,
    account_ids: list[int],
    group: TgGroup,
) -> dict[int, tuple[str, str, str]]:
    if not account_ids:
        return {}
    accounts = session.scalars(select(TgAccount).where(TgAccount.id.in_(account_ids)))
    links = session.scalars(select(TgGroupAccount).where(
        TgGroupAccount.tenant_id == group.tenant_id,
        TgGroupAccount.group_id == group.id,
        TgGroupAccount.account_id.in_(account_ids),
    ))
    admissions = session.scalars(select(GroupBotAdmission).where(
        GroupBotAdmission.tenant_id == group.tenant_id,
        GroupBotAdmission.group_id == group.id,
        GroupBotAdmission.account_id.in_(account_ids),
    ))
    account_by_id = {account.id: account for account in accounts}
    link_by_account = {link.account_id: link for link in links}
    admission_rows = list(admissions)
    admission_by_account = {row.account_id: row for row in admission_rows}
    plannable_ids = plannable_admission_account_ids(session, admission_rows)
    return {
        account_id: _readiness_from_records(
            account_by_id.get(account_id),
            link_by_account.get(account_id),
            admission_by_account.get(account_id),
            post_follow_probe_eligible=account_id in plannable_ids,
        )
        for account_id in account_ids
    }


def _readiness_from_records(
    account: TgAccount | None,
    link: TgGroupAccount | None,
    admission: GroupBotAdmission | None,
    *,
    post_follow_probe_eligible: bool,
) -> tuple[str, str, str]:
    if account is None or account.deleted_at is not None:
        return "blocked", "account_deleted", "账号已删除"
    if account.status != AccountStatus.ACTIVE.value:
        return "blocked", _status_blocker(account.status), f"账号状态：{account.status}"
    try:
        session_ready = bool(decrypt_session(account.session_ciphertext))
    except Exception as exc:
        return "blocked", "session_invalid", str(exc)
    if not session_ready:
        return "blocked", "session_missing", "账号缺少可用 Session"
    if link is None:
        return "pending_admission", "not_in_group", "账号尚未进入目标群"
    if not link.can_send:
        return "blocked", "cannot_send", "账号在目标群不可发言"
    if admission is not None and not post_follow_probe_eligible:
        return (
            "pending_admission",
            "group_bot_admission_wait",
            f"群管机器人准入未完成：{admission.state}",
        )
    return "ready", "", ""


def _status_blocker(status: str) -> str:
    mapping = {
        AccountStatus.SESSION_EXPIRED.value: "session_expired",
        AccountStatus.NEED_RELOGIN.value: "need_relogin",
        AccountStatus.LIMITED.value: "account_limited",
        AccountStatus.BANNED.value: "account_banned",
        AccountStatus.DISABLED.value: "account_disabled",
    }
    return mapping.get(status, "account_offline")


def _sync_item_phase(item: TaskMembershipAdmissionItem, state: str, code: str, detail: str) -> None:
    if state == "ready":
        item.phase = "completed"
        item.failure_type = ""
        item.failure_detail = ""
        return
    if state == "pending_admission":
        item.phase = "pending"
        return
    item.phase = "failed"
    item.failure_type = code
    item.failure_detail = detail
