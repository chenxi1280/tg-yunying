from __future__ import annotations

import json
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountStatus, AuditLog, TgAccount, TgAccountSecurityBatch, TgAccountSecurityBatchItem
from app.schemas.account_security import AccountSecurityBatchCreate, AvatarStrategy, ProfileGenerationStrategy
from app.services.account_security import create_account_security_batch


AUTO_PROFILE_ACTIONS = ["update_profile", "update_username", "update_avatar"]
AUTO_PROFILE_REASON = "登录成功后自动初始化账号中文资料和头像"
AUTO_VOICE_PROFILE_REASON = "登录成功后自动初始化账号表达卡"
AUTO_PROFILE_ACTOR = "account-profile-auto-init"
OPEN_BATCH_STATUSES = {"ready", "running"}
OPEN_ITEM_STATUSES = {"pending", "running", "waiting"}
GENERIC_DISPLAY_NAMES = {"", "托管账号", "新托管账号", "未命名账号"}
SYSTEM_DISPLAY_NAME_RE = re.compile(r"^导入\d{4}-\d{2,4}-\d{3}$")
ASCII_LETTER_RE = re.compile(r"[A-Za-z]")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


@dataclass(frozen=True)
class ProfileInitializationQueueResult:
    inspected_count: int
    queued_account_ids: tuple[int, ...]
    batch_ids: tuple[int, ...]


def queue_login_profile_initialization(session: Session, account_id: int, actor: str) -> ProfileInitializationQueueResult:
    account = session.get(TgAccount, account_id)
    if not account:
        return ProfileInitializationQueueResult(inspected_count=0, queued_account_ids=(), batch_ids=())
    return queue_profile_initialization_for_accounts(
        session,
        tenant_id=account.tenant_id,
        account_ids=[account.id],
        actor=actor,
        reason=AUTO_PROFILE_REASON,
    )


def queue_profile_initialization_for_accounts(
    session: Session,
    *,
    tenant_id: int,
    account_ids: list[int] | None = None,
    actor: str = AUTO_PROFILE_ACTOR,
    reason: str = AUTO_PROFILE_REASON,
) -> ProfileInitializationQueueResult:
    accounts = _candidate_accounts(session, tenant_id, account_ids)
    selected = [account for account in accounts if _should_queue_account(session, account)]
    _queue_voice_profile_initialization(session, tenant_id, accounts, actor)
    batch_ids: list[int] = []
    for overwrite_existing in (False, True):
        group = [account for account in selected if _requires_name_overwrite(account) is overwrite_existing]
        if group:
            batch = create_account_security_batch(session, tenant_id, _batch_payload(group, overwrite_existing, reason), actor)
            batch_ids.append(batch.id)
    return ProfileInitializationQueueResult(
        inspected_count=len(accounts),
        queued_account_ids=tuple(account.id for account in selected),
        batch_ids=tuple(batch_ids),
    )


def _queue_voice_profile_initialization(session: Session, tenant_id: int, accounts: list[TgAccount], actor: str) -> None:
    if not accounts:
        return
    account_ids = [account.id for account in accounts]
    try:
        created = _ensure_voice_profiles(session, tenant_id, account_ids)
        _audit_voice_profile_init(session, tenant_id, actor, "账号表达卡初始化", account_ids, f"created={created}")
    except (RuntimeError, ValueError) as exc:
        _audit_voice_profile_init(session, tenant_id, actor, "账号表达卡初始化失败", account_ids, str(exc))


def _ensure_voice_profiles(session: Session, tenant_id: int, account_ids: list[int]) -> int:
    from app.services.task_center.account_voice_profiles import ensure_voice_profiles_for_accounts, generate_voice_profiles_with_ai

    generator = generate_voice_profiles_with_ai(session, tenant_id=tenant_id)
    return ensure_voice_profiles_for_accounts(session, tenant_id=tenant_id, account_ids=account_ids, generator=generator)


def _audit_voice_profile_init(session: Session, tenant_id: int, actor: str, action: str, account_ids: list[int], detail: str) -> None:
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor=actor,
            action=action,
            target_type="ai_account_voice_profile",
            target_id=",".join(str(account_id) for account_id in account_ids),
            detail=detail,
        )
    )


def profile_is_ready(account: TgAccount) -> bool:
    return bool(
        _is_chinese_text(account.display_name)
        and _is_chinese_text(account.tg_first_name)
        and not _has_ascii_letters(account.tg_last_name)
        and str(account.username or "").strip()
        and str(account.avatar_object_key or "").strip()
    )


def _candidate_accounts(session: Session, tenant_id: int, account_ids: list[int] | None) -> list[TgAccount]:
    stmt = select(TgAccount).where(
        TgAccount.tenant_id == tenant_id,
        TgAccount.deleted_at.is_(None),
        TgAccount.status == AccountStatus.ACTIVE.value,
        TgAccount.session_ciphertext.is_not(None),
        TgAccount.session_ciphertext != "",
    )
    if account_ids:
        stmt = stmt.where(TgAccount.id.in_(account_ids))
    return list(session.scalars(stmt.order_by(TgAccount.id.asc())))


def _should_queue_account(session: Session, account: TgAccount) -> bool:
    return not profile_is_ready(account) and not _has_open_profile_initialization(session, account.id)


def _has_open_profile_initialization(session: Session, account_id: int) -> bool:
    rows = session.execute(
        select(TgAccountSecurityBatch, TgAccountSecurityBatchItem)
        .join(TgAccountSecurityBatchItem, TgAccountSecurityBatchItem.batch_id == TgAccountSecurityBatch.id)
        .where(
            TgAccountSecurityBatchItem.account_id == account_id,
            TgAccountSecurityBatch.status.in_(OPEN_BATCH_STATUSES),
            TgAccountSecurityBatchItem.status.in_(OPEN_ITEM_STATUSES),
        )
    )
    return any(set(_json_list(batch.action_types)) & set(AUTO_PROFILE_ACTIONS) for batch, _item in rows)


def _batch_payload(accounts: list[TgAccount], overwrite_existing: bool, reason: str) -> AccountSecurityBatchCreate:
    return AccountSecurityBatchCreate(
        account_ids=[account.id for account in accounts],
        action_types=AUTO_PROFILE_ACTIONS,
        confirm_text="确认",
        reason=reason,
        profile_strategy=ProfileGenerationStrategy(
            generation_mode="local_random",
            language_style="中文",
            persona_style="自然用户",
            bio_enabled=True,
            username_enabled=True,
            overwrite_existing=overwrite_existing,
        ),
        avatar_strategy=AvatarStrategy(mode="material_random"),
    )


def _requires_name_overwrite(account: TgAccount) -> bool:
    display_name = str(account.display_name or "").strip()
    first_name = str(account.tg_first_name or "").strip()
    last_name = str(account.tg_last_name or "").strip()
    return bool(_is_replaceable_display_name(display_name) or not _is_chinese_text(display_name) or not _is_chinese_text(first_name) or _has_ascii_letters(last_name))


def _is_replaceable_display_name(value: str) -> bool:
    return value in GENERIC_DISPLAY_NAMES or bool(SYSTEM_DISPLAY_NAME_RE.match(value))


def _is_chinese_text(value: str | None) -> bool:
    text = str(value or "").strip()
    return bool(text and CJK_RE.search(text) and not ASCII_LETTER_RE.search(text))


def _has_ascii_letters(value: str | None) -> bool:
    return bool(ASCII_LETTER_RE.search(str(value or "")))


def _json_list(value: str) -> list[str]:
    try:
        data = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


__all__ = [
    "AUTO_PROFILE_REASON",
    "ProfileInitializationQueueResult",
    "profile_is_ready",
    "queue_login_profile_initialization",
    "queue_profile_initialization_for_accounts",
]
