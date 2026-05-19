from __future__ import annotations

import json
import re
import secrets
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import or_, func, select
from sqlalchemy.orm import Session

from app.models import (
    AccountStatus,
    AiProvider,
    AiProviderHealthStatus,
    Material,
    TgAccount,
    TgAccountAuthorizationSnapshot,
    TgAccountProfileBatchRule,
    TgAccountSecurityBatch,
    TgAccountSecurityBatchItem,
    TgAccountSecuritySnapshot,
)
from app.schemas.account_security import (
    AccountSecurityBatchCreate,
    AccountSecurityBatchOut,
    AccountSecurityDetailOut,
    AccountSecurityPrecheckOut,
    AccountSecurityPrecheckRequest,
    AccountSecurityPreviewItem,
    AccountSecurityRetryRequest,
    AccountSecuritySummaryOut,
)
from app.security import decrypt_secret, encrypt_secret
from app.storage import media_root, object_path, save_avatar_bytes

from .._common import _now, ai_gateway, audit, gateway, require_tenant
from ..ai_config import ai_provider_credentials, get_tenant_ai_setting
from ..developer_apps import credentials_for_account


USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
SYSTEM_DISPLAY_NAME_RE = re.compile(r"^导入\d{4}-\d{2,4}-\d{3}$")
GENERIC_DISPLAY_NAMES = {"", "托管账号", "新托管账号", "未命名账号"}
PROFILE_AI_BASE_TIMEOUT_SECONDS = 45
PROFILE_AI_MAX_TIMEOUT_SECONDS = 180
PROFILE_ACTIONS = {"update_profile", "update_username", "update_avatar"}
SECURITY_ACTIONS = {"cleanup_devices", "set_two_fa"}
ALL_ACTIONS = PROFILE_ACTIONS | SECURITY_ACTIONS
AI_NAME_POOL = [
    ("锅巴洋芋", "", "日常在线，随缘交流"),
    ("蕉太狼", "", "偶尔冒泡，看到就回"),
    ("早睡失败", "", "分享一点生活碎片"),
    ("小熊便利店", "", "路过看看，随手聊聊"),
    ("不吃香菜", "", "慢慢看，慢慢聊"),
    ("月亮打烊", "", "喜欢记录一些小事"),
    ("西瓜汽水", "", "看到有意思的会回两句"),
    ("今天也很困", "", "在线时间不固定"),
    ("糯米团子", "", "随缘交流，别太正式"),
    ("橘子海", "", "爱看新鲜事"),
]


def _json_list(value: str) -> list[str]:
    try:
        data = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _json_dict(value: str) -> dict:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _snapshot(session: Session, account: TgAccount) -> TgAccountSecuritySnapshot:
    snapshot = session.scalar(select(TgAccountSecuritySnapshot).where(TgAccountSecuritySnapshot.account_id == account.id))
    if snapshot:
        return snapshot
    snapshot = TgAccountSecuritySnapshot(tenant_id=account.tenant_id, account_id=account.id)
    session.add(snapshot)
    session.flush()
    return snapshot


def _mask_ip(value: str) -> str:
    if not value or "." not in value:
        return value
    parts = value.split(".")
    if len(parts) == 4:
        return f"{parts[0]}.{parts[1]}.*.*"
    return value


def _batch_out(batch: TgAccountSecurityBatch, items: list[TgAccountSecurityBatchItem] | None = None) -> AccountSecurityBatchOut:
    return AccountSecurityBatchOut(
        id=batch.id,
        tenant_id=batch.tenant_id,
        action_types=_json_list(batch.action_types),
        status=batch.status,
        total_count=batch.total_count,
        success_count=batch.success_count,
        skipped_count=batch.skipped_count,
        failed_count=batch.failed_count,
        created_by=batch.created_by,
        confirmed_by=batch.confirmed_by,
        confirm_text=batch.confirm_text,
        password_strategy=batch.password_strategy,
        profile_strategy=_json_dict(batch.profile_strategy),
        username_strategy=_json_dict(batch.username_strategy),
        avatar_strategy=_json_dict(batch.avatar_strategy),
        overwrite_existing_profile=batch.overwrite_existing_profile,
        reason=batch.reason,
        trace_id=batch.trace_id,
        created_at=batch.created_at,
        started_at=batch.started_at,
        finished_at=batch.finished_at,
        items=[_item_out(item) for item in (items or [])],
    )


def _item_out(item: TgAccountSecurityBatchItem):
    return {
        "id": item.id,
        "batch_id": item.batch_id,
        "tenant_id": item.tenant_id,
        "account_id": item.account_id,
        "status": item.status,
        "precheck_status": item.precheck_status,
        "cleanup_status": item.cleanup_status,
        "two_fa_status": item.two_fa_status,
        "profile_status": item.profile_status,
        "username_status": item.username_status,
        "avatar_status": item.avatar_status,
        "external_devices_before": item.external_devices_before,
        "external_devices_after": item.external_devices_after,
        "generated_display_name": item.generated_display_name,
        "generated_first_name": item.generated_first_name,
        "generated_last_name": item.generated_last_name,
        "generated_bio": item.generated_bio,
        "generated_username": item.generated_username,
        "username_candidates": _json_list(item.username_candidates),
        "avatar_source": item.avatar_source,
        "skipped_reason": item.skipped_reason,
        "failure_type": item.failure_type,
        "failure_detail": item.failure_detail,
        "next_retry_at": item.next_retry_at,
        "trace_id": item.trace_id,
        "created_at": item.created_at,
        "started_at": item.started_at,
        "finished_at": item.finished_at,
    }


def account_security_summary(session: Session, tenant_id: int) -> AccountSecuritySummaryOut:
    total_accounts = session.scalar(select(func.count(TgAccount.id)).where(TgAccount.tenant_id == tenant_id, TgAccount.deleted_at.is_(None))) or 0
    external = session.scalar(
        select(func.count(TgAccountSecuritySnapshot.id)).where(
            TgAccountSecuritySnapshot.tenant_id == tenant_id,
            TgAccountSecuritySnapshot.external_authorization_count > 0,
        )
    ) or 0
    missing_2fa = session.scalar(
        select(func.count(TgAccountSecuritySnapshot.id)).where(
            TgAccountSecuritySnapshot.tenant_id == tenant_id,
            TgAccountSecuritySnapshot.two_fa_status.in_(["missing", "unknown", "failed"]),
        )
    ) or 0
    incomplete_profile = session.scalar(
        select(func.count(TgAccountSecuritySnapshot.id)).where(
            TgAccountSecuritySnapshot.tenant_id == tenant_id,
            TgAccountSecuritySnapshot.profile_status.in_(["unknown", "incomplete", "update_failed"]),
        )
    ) or 0
    recent_failed = session.scalar(
        select(func.count(TgAccountSecurityBatch.id)).where(
            TgAccountSecurityBatch.tenant_id == tenant_id,
            TgAccountSecurityBatch.status.in_(["failed", "partial_success"]),
        )
    ) or 0
    pending = session.scalar(
        select(func.count(TgAccountSecurityBatch.id)).where(
            TgAccountSecurityBatch.tenant_id == tenant_id,
            TgAccountSecurityBatch.status.in_(["draft", "ready", "running"]),
        )
    ) or 0
    return AccountSecuritySummaryOut(
        total_accounts=total_accounts,
        external_device_accounts=external,
        missing_two_fa_accounts=missing_2fa,
        incomplete_profile_accounts=incomplete_profile,
        recent_failed_batches=recent_failed,
        pending_batches=pending,
    )


def refresh_account_security(session: Session, tenant_id: int, account_id: int, actor: str = "system") -> TgAccountSecuritySnapshot:
    account = session.get(TgAccount, account_id)
    if not account or account.tenant_id != tenant_id or account.deleted_at is not None:
        raise ValueError("account not found")
    snapshot = _snapshot(session, account)
    now_value = _now()
    try:
        credentials = credentials_for_account(session, account)
        authorizations = gateway.list_authorizations(account.session_ciphertext, credentials)
    except Exception as exc:  # noqa: BLE001 - operator-facing security status.
        snapshot.trusted_session_status = "unknown"
        snapshot.last_error = str(exc)
        session.commit()
        return snapshot

    session.query(TgAccountAuthorizationSnapshot).filter(TgAccountAuthorizationSnapshot.account_id == account.id).delete()
    external_count = 0
    trusted = False
    for authorization in authorizations:
        is_current = bool(authorization.is_current)
        trusted = trusted or is_current
        if not is_current:
            external_count += 1
        session.add(
            TgAccountAuthorizationSnapshot(
                tenant_id=account.tenant_id,
                account_id=account.id,
                authorization_hash_ciphertext=encrypt_secret(authorization.authorization_hash),
                is_platform_trusted=is_current,
                is_current_session=is_current,
                device_model=authorization.device_model,
                platform=authorization.platform,
                system_version=authorization.system_version,
                api_id=authorization.api_id,
                app_name=authorization.app_name,
                app_version=authorization.app_version,
                ip_masked=_mask_ip(authorization.ip),
                country=authorization.country,
                region=authorization.region,
                date_created=authorization.date_created,
                date_active=authorization.date_active,
                scanned_at=now_value,
            )
        )
    two_fa = gateway.get_two_fa_status(account.session_ciphertext, credentials)
    snapshot.trusted_session_status = "confirmed" if trusted else "missing"
    snapshot.two_fa_status = two_fa.status if two_fa.ok else "unknown"
    snapshot.external_authorization_count = external_count
    snapshot.last_device_scan_at = now_value
    snapshot.last_2fa_check_at = now_value
    snapshot.profile_status = _profile_status(account)
    snapshot.profile_last_updated_at = account.profile_synced_at
    snapshot.trusted_device_label = "TG运营平台-主控"
    snapshot.last_error = ""
    snapshot.trace_id = uuid4().hex
    audit(session, tenant_id=tenant_id, actor=actor, action="刷新账号安全状态", target_type="tg_account", target_id=str(account.id), detail=snapshot.trace_id)
    session.commit()
    session.refresh(snapshot)
    return snapshot


def _profile_status(account: TgAccount) -> str:
    if account.avatar_object_key and account.tg_first_name and account.username:
        return "complete"
    return "incomplete"


def _account_profile_preview(account: TgAccount) -> dict[str, object]:
    username_candidates = [account.username] if account.username else []
    return {
        "display_name": account.display_name or "",
        "first_name": account.tg_first_name or "",
        "last_name": account.tg_last_name or "",
        "bio": account.tg_bio or "",
        "username_candidates": username_candidates,
        "avatar_source": f"avatar:{account.avatar_object_key}" if account.avatar_object_key else "",
    }


def account_security_detail(session: Session, tenant_id: int, account_id: int) -> AccountSecurityDetailOut:
    account = session.get(TgAccount, account_id)
    if not account or account.tenant_id != tenant_id or account.deleted_at is not None:
        raise ValueError("account not found")
    snapshot = _snapshot(session, account)
    authorizations = list(
        session.scalars(
            select(TgAccountAuthorizationSnapshot)
            .where(TgAccountAuthorizationSnapshot.tenant_id == tenant_id, TgAccountAuthorizationSnapshot.account_id == account_id)
            .order_by(TgAccountAuthorizationSnapshot.is_current_session.desc(), TgAccountAuthorizationSnapshot.id.desc())
        )
    )
    batches = list(
        session.scalars(
            select(TgAccountSecurityBatch)
            .join(TgAccountSecurityBatchItem, TgAccountSecurityBatchItem.batch_id == TgAccountSecurityBatch.id)
            .where(TgAccountSecurityBatch.tenant_id == tenant_id, TgAccountSecurityBatchItem.account_id == account_id)
            .order_by(TgAccountSecurityBatch.id.desc())
            .limit(5)
        )
    )
    return AccountSecurityDetailOut(
        account_id=account_id,
        snapshot=snapshot,
        authorizations=authorizations,
        recent_batches=[_batch_out(batch) for batch in batches],
    )


def precheck_account_security_batch(session: Session, tenant_id: int, payload: AccountSecurityPrecheckRequest) -> AccountSecurityPrecheckOut:
    require_tenant(session, tenant_id)
    action_types = _valid_actions(payload.action_types)
    accounts = _accounts_for_payload(session, tenant_id, payload.account_ids)
    trace_id = uuid4().hex
    items: list[AccountSecurityPreviewItem] = []
    needs_profile_preview = bool(set(action_types) & PROFILE_ACTIONS)
    generated = _generate_profiles(session, tenant_id, accounts, payload.profile_strategy) if needs_profile_preview else [_account_profile_preview(account) for account in accounts]
    overrides = {override.account_id: override for override in payload.preview_overrides}
    for index, account in enumerate(accounts):
        if set(action_types) & SECURITY_ACTIONS:
            try:
                snapshot = refresh_account_security(session, tenant_id, account.id, actor="precheck")
            except ValueError:
                snapshot = _snapshot(session, account)
        else:
            snapshot = _snapshot(session, account)
        blockers: list[str] = []
        warnings: list[str] = []
        suggested: list[str] = []
        status = "executable"
        if account.status != AccountStatus.ACTIVE.value or not account.session_ciphertext:
            blockers.append("账号未在线或缺少可用 session")
            suggested.append("先重新登录账号")
            status = "manual_required"
        wait_until = _fresh_session_wait_until(session, account) if "cleanup_devices" in action_types else None
        if wait_until:
            blockers.append(f"新登录 Session 未满 24 小时，需等待到 {wait_until.isoformat()}")
            suggested.append("等待 Telegram 安全限制解除后重试设备清理")
            status = "waiting"
        if "set_two_fa" in action_types and snapshot.two_fa_status == "enabled":
            warnings.append("账号已设置二步验证，将跳过 2FA 设置")
        override = overrides.get(account.id)
        generated_item = _apply_preview_override(generated[index], override)
        if generated_item.get("generation_error"):
            message = str(generated_item["generation_error"])
            if override:
                warnings.append(f"{message}；已使用本次手工编辑预览")
            else:
                blockers.append(message)
                suggested.append("切换到模板兜底、导入名单，或手工编辑预览后再确认")
                status = "manual_required"
        if generated_item.get("generation_warning"):
            warnings.append(str(generated_item["generation_warning"]))
        username_candidates = generated_item["username_candidates"] if "update_username" in action_types else []
        invalid_usernames = [candidate for candidate in username_candidates if not USERNAME_RE.match(candidate)]
        if invalid_usernames:
            blockers.append(f"username 候选格式错误：{','.join(invalid_usernames)}")
            status = "manual_required"
        avatar_source = str(generated_item.get("avatar_source") or _avatar_source(index, payload.avatar_strategy))
        if "update_avatar" in action_types:
            avatar_error = _validate_avatar_source(session, account, avatar_source)
            if avatar_error:
                warnings.append(avatar_error)
                avatar_source = ""
        items.append(
            AccountSecurityPreviewItem(
                account_id=account.id,
                account_name=account.display_name,
                phone_masked=account.phone_masked,
                session_status=account.status,
                trusted_session_status=snapshot.trusted_session_status,
                external_authorization_count=snapshot.external_authorization_count,
                two_fa_status=snapshot.two_fa_status,
                profile_status=snapshot.profile_status,
                generated_display_name=generated_item["display_name"],
                generated_first_name=generated_item["first_name"],
                generated_last_name=generated_item["last_name"],
                generated_bio=generated_item["bio"],
                username_candidates=username_candidates,
                avatar_source=avatar_source,
                precheck_status=status,
                blockers=blockers,
                warnings=warnings,
                suggested_actions=suggested,
            )
        )
    summary = {
        "total": len(items),
        "executable": sum(1 for item in items if item.precheck_status == "executable"),
        "skipped": sum(1 for item in items if item.precheck_status == "skipped"),
        "manual_required": sum(1 for item in items if item.precheck_status == "manual_required"),
        "waiting": sum(1 for item in items if item.precheck_status == "waiting"),
    }
    return AccountSecurityPrecheckOut(batch_preview_id=f"preview_{trace_id[:10]}", summary=summary, items=items, action_types=action_types, trace_id=trace_id)


def create_account_security_batch(session: Session, tenant_id: int, payload: AccountSecurityBatchCreate, actor: str) -> AccountSecurityBatchOut:
    preview = precheck_account_security_batch(session, tenant_id, payload)
    confirmed = payload.confirm_text == "确认加固"
    initial_status = "running" if confirmed and preview.summary.get("executable", 0) > 0 else "ready"
    batch = TgAccountSecurityBatch(
        tenant_id=tenant_id,
        action_types=json.dumps(preview.action_types, ensure_ascii=False),
        status=initial_status,
        total_count=len(preview.items),
        created_by=actor,
        confirmed_by=actor if confirmed else "",
        confirm_text=payload.confirm_text,
        password_strategy=payload.password_strategy,
        profile_strategy=payload.profile_strategy.model_dump_json(),
        username_strategy=json.dumps({"mode": payload.profile_strategy.generation_mode}, ensure_ascii=False),
        avatar_strategy=payload.avatar_strategy.model_dump_json(),
        overwrite_existing_profile=payload.profile_strategy.overwrite_existing,
        reason=payload.reason,
        trace_id=preview.trace_id,
        started_at=_now() if initial_status == "running" else None,
    )
    session.add(batch)
    session.flush()
    session.add(
        TgAccountProfileBatchRule(
            batch_id=batch.id,
            tenant_id=tenant_id,
            generation_mode=payload.profile_strategy.generation_mode,
            language_style=payload.profile_strategy.language_style,
            persona_style=payload.profile_strategy.persona_style,
            gender_bias=payload.profile_strategy.gender_bias,
            age_style=payload.profile_strategy.age_style,
            forbidden_words=",".join(payload.profile_strategy.forbidden_words),
            uniqueness_seed=preview.trace_id,
            username_prefix=payload.profile_strategy.username_prefix_hint,
            username_max_attempts=payload.profile_strategy.username_max_attempts,
            avatar_assignment_mode=payload.avatar_strategy.mode,
            overwrite_existing=payload.profile_strategy.overwrite_existing,
        )
    )
    for preview_item in preview.items:
        item_status = "pending" if preview_item.precheck_status == "executable" and batch.status == "running" else preview_item.precheck_status
        item = TgAccountSecurityBatchItem(
            batch_id=batch.id,
            tenant_id=tenant_id,
            account_id=preview_item.account_id,
            status=item_status,
            precheck_status=preview_item.precheck_status,
            cleanup_status="pending" if "cleanup_devices" in preview.action_types and item_status == "pending" else "not_requested",
            two_fa_status="pending" if "set_two_fa" in preview.action_types and item_status == "pending" else "not_requested",
            profile_status="pending" if "update_profile" in preview.action_types and item_status == "pending" else "not_requested",
            username_status="pending" if "update_username" in preview.action_types and item_status == "pending" else "not_requested",
            avatar_status="pending" if "update_avatar" in preview.action_types and item_status == "pending" else "not_requested",
            external_devices_before=preview_item.external_authorization_count,
            generated_display_name=preview_item.generated_display_name,
            generated_first_name=preview_item.generated_first_name,
            generated_last_name=preview_item.generated_last_name,
            generated_bio=preview_item.generated_bio,
            username_candidates=json.dumps(preview_item.username_candidates, ensure_ascii=False),
            avatar_source=preview_item.avatar_source,
            skipped_reason=";".join(preview_item.blockers),
            trace_id=preview.trace_id,
        )
        session.add(item)
    audit(session, tenant_id=tenant_id, actor=actor, action="创建账号安全加固批次", target_type="account_security_batch", target_id=str(batch.id), detail=payload.reason)
    session.commit()
    return account_security_batch_detail(session, tenant_id, batch.id)


def list_account_security_batches(session: Session, tenant_id: int, limit: int = 50) -> list[AccountSecurityBatchOut]:
    batches = list(
        session.scalars(
            select(TgAccountSecurityBatch)
            .where(TgAccountSecurityBatch.tenant_id == tenant_id)
            .order_by(TgAccountSecurityBatch.id.desc())
            .limit(limit)
        )
    )
    return [_batch_out(batch) for batch in batches]


def account_security_batch_detail(session: Session, tenant_id: int, batch_id: int) -> AccountSecurityBatchOut:
    batch = session.get(TgAccountSecurityBatch, batch_id)
    if not batch or batch.tenant_id != tenant_id:
        raise ValueError("batch not found")
    items = list(session.scalars(select(TgAccountSecurityBatchItem).where(TgAccountSecurityBatchItem.batch_id == batch.id).order_by(TgAccountSecurityBatchItem.id.asc())))
    return _batch_out(batch, items)


def retry_account_security_batch(session: Session, tenant_id: int, batch_id: int, payload: AccountSecurityRetryRequest, actor: str) -> AccountSecurityBatchOut:
    batch = session.get(TgAccountSecurityBatch, batch_id)
    if not batch or batch.tenant_id != tenant_id:
        raise ValueError("batch not found")
    stmt = select(TgAccountSecurityBatchItem).where(TgAccountSecurityBatchItem.batch_id == batch.id, TgAccountSecurityBatchItem.status.in_(["failed", "partial_success", "waiting"]))
    if payload.item_ids:
        stmt = stmt.where(TgAccountSecurityBatchItem.id.in_(payload.item_ids))
    for item in session.scalars(stmt):
        item.status = "pending"
        item.failure_type = ""
        item.failure_detail = ""
        item.next_retry_at = None
    batch.status = "running"
    batch.started_at = batch.started_at or _now()
    batch.finished_at = None
    audit(session, tenant_id=tenant_id, actor=actor, action="重试账号安全加固批次", target_type="account_security_batch", target_id=str(batch.id))
    session.commit()
    return account_security_batch_detail(session, tenant_id, batch.id)


def cancel_account_security_batch(session: Session, tenant_id: int, batch_id: int, actor: str) -> AccountSecurityBatchOut:
    batch = session.get(TgAccountSecurityBatch, batch_id)
    if not batch or batch.tenant_id != tenant_id:
        raise ValueError("batch not found")
    for item in session.scalars(select(TgAccountSecurityBatchItem).where(TgAccountSecurityBatchItem.batch_id == batch.id, TgAccountSecurityBatchItem.status == "pending")):
        item.status = "skipped"
        item.skipped_reason = "批次已取消"
    batch.status = "cancelled"
    batch.finished_at = _now()
    audit(session, tenant_id=tenant_id, actor=actor, action="取消账号安全加固批次", target_type="account_security_batch", target_id=str(batch.id))
    session.commit()
    return account_security_batch_detail(session, tenant_id, batch.id)


def drain_account_security_batches(session_factory, limit: int = 20) -> int:
    processed = 0
    now_value = _now()
    with session_factory() as session:
        item_ids = list(
            session.scalars(
                select(TgAccountSecurityBatchItem.id)
                .join(TgAccountSecurityBatch, TgAccountSecurityBatch.id == TgAccountSecurityBatchItem.batch_id)
                .where(
                    TgAccountSecurityBatch.status == "running",
                    or_(
                        TgAccountSecurityBatchItem.status == "pending",
                        (
                            (TgAccountSecurityBatchItem.status == "waiting")
                            & (
                                TgAccountSecurityBatchItem.next_retry_at.is_(None)
                                | (TgAccountSecurityBatchItem.next_retry_at <= now_value)
                            )
                        ),
                    ),
                )
                .order_by(TgAccountSecurityBatchItem.id.asc())
                .limit(limit)
            )
        )
    for item_id in item_ids:
        with session_factory() as session:
            _execute_batch_item(session, item_id)
            processed += 1
    return processed


def _execute_batch_item(session: Session, item_id: int) -> None:
    item = session.get(TgAccountSecurityBatchItem, item_id)
    if not item:
        return
    if item.status == "waiting":
        if item.next_retry_at and item.next_retry_at > _now():
            return
        item.status = "pending"
    if item.status != "pending":
        return
    batch = session.get(TgAccountSecurityBatch, item.batch_id)
    account = session.get(TgAccount, item.account_id)
    if not batch or not account or account.deleted_at is not None:
        if item:
            item.status = "failed"
            item.failure_type = "账号不可用"
            item.failure_detail = "账号不存在或已删除"
            item.finished_at = _now()
            session.commit()
        return
    other_running = session.scalar(
        select(func.count(TgAccountSecurityBatchItem.id)).where(
            TgAccountSecurityBatchItem.account_id == item.account_id,
            TgAccountSecurityBatchItem.id != item.id,
            TgAccountSecurityBatchItem.status == "running",
        )
    ) or 0
    if other_running:
        item.status = "waiting"
        item.failure_type = "账号正在执行其他加固项"
        item.failure_detail = "同一账号同一时间只允许一个安全加固批次项执行"
        item.next_retry_at = _now() + timedelta(minutes=1)
        session.commit()
        return
    action_types = set(_json_list(batch.action_types))
    item.status = "running"
    item.started_at = _now()
    session.commit()
    failures: list[str] = []
    try:
        credentials = credentials_for_account(session, account)
        if "cleanup_devices" in action_types:
            failures.extend(_execute_cleanup(session, account, item, credentials))
        if "set_two_fa" in action_types:
            generated_password = _generate_two_fa_password(account, item)
            result = gateway.set_two_fa_password(account.session_ciphertext, generated_password, credentials=credentials, hint="TG运营平台托管")
            item.two_fa_status = result.status if result.ok else "failed"
            if not result.ok:
                failures.append(result.detail or result.failure_type)
            else:
                snapshot = _snapshot(session, account)
                snapshot.two_fa_status = "enabled"
                snapshot.two_fa_password_ciphertext = encrypt_secret(generated_password)
                snapshot.two_fa_password_hint = "TG运营平台托管"
                snapshot.two_fa_password_stored_at = _now()
        if "update_profile" in action_types:
            display_name = item.generated_display_name if batch.overwrite_existing_profile or _can_replace_display_name(account.display_name) else account.display_name
            first_name = item.generated_first_name if batch.overwrite_existing_profile or not account.tg_first_name else account.tg_first_name
            last_name = item.generated_last_name if batch.overwrite_existing_profile or not account.tg_last_name else account.tg_last_name
            bio = item.generated_bio if batch.overwrite_existing_profile or not account.tg_bio else account.tg_bio
            profile_result = gateway.update_profile(
                account.session_ciphertext,
                first_name=first_name or display_name or account.display_name,
                last_name=last_name,
                bio=bio,
                credentials=credentials,
            )
            item.profile_status = "succeeded" if profile_result.ok else "failed"
            if profile_result.ok:
                account.display_name = display_name or account.display_name
                account.tg_first_name = first_name or account.tg_first_name
                account.tg_last_name = last_name or account.tg_last_name
                account.tg_bio = bio or account.tg_bio
                account.profile_sync_status = "已同步"
                account.profile_sync_error = ""
                account.profile_synced_at = _now()
            else:
                failures.append(profile_result.detail)
        if "update_username" in action_types:
            if account.username and not batch.overwrite_existing_profile:
                username = account.username
                item.username_status = "skipped"
                item.generated_username = username
            else:
                username = _try_usernames(account, item, credentials)
                item.username_status = "succeeded" if username else "failed"
                if username:
                    account.username = username
                    item.generated_username = username
                else:
                    failures.append("username 候选均不可用")
        if "update_avatar" in action_types:
            avatar_result = _execute_avatar_update(session, account, item, credentials, overwrite_existing=batch.overwrite_existing_profile)
            item.avatar_status = avatar_result.status if avatar_result.ok else "failed"
            if not avatar_result.ok and avatar_result.status != "skipped":
                failures.append(avatar_result.detail or avatar_result.failure_type)
        snapshot = _snapshot(session, account)
        snapshot.profile_status = _profile_status(account)
        snapshot.last_hardened_at = _now()
        snapshot.last_error = ";".join(failures)
        item.status = "waiting" if failures and item.next_retry_at and not _item_has_success(item) else "partial_success" if failures and _item_has_success(item) else "failed" if failures else "succeeded"
        item.failure_type = "需等待" if item.status == "waiting" else "部分失败" if item.status == "partial_success" else "执行失败" if failures else ""
        item.failure_detail = ";".join(failures)
    except Exception as exc:  # noqa: BLE001 - operator-facing batch failure.
        item.status = "failed"
        item.failure_type = "执行异常"
        item.failure_detail = str(exc)
    item.finished_at = _now()
    _refresh_batch_counts(session, batch)
    audit(session, tenant_id=batch.tenant_id, actor="account-security-worker", action="执行账号安全加固项", target_type="account_security_batch_item", target_id=str(item.id), detail=item.status)
    session.commit()


def _execute_cleanup(session: Session, account: TgAccount, item: TgAccountSecurityBatchItem, credentials) -> list[str]:
    failures: list[str] = []
    authorizations = list(
        session.scalars(
            select(TgAccountAuthorizationSnapshot).where(TgAccountAuthorizationSnapshot.account_id == account.id)
        )
    )
    if not authorizations:
        refresh_account_security(session, account.tenant_id, account.id, actor="account-security-worker")
        authorizations = list(session.scalars(select(TgAccountAuthorizationSnapshot).where(TgAccountAuthorizationSnapshot.account_id == account.id)))
    external = [authorization for authorization in authorizations if not authorization.is_current_session]
    item.external_devices_before = len(external)
    cleaned = 0
    for authorization in external:
        raw_hash = decrypt_secret(authorization.authorization_hash_ciphertext) or authorization.authorization_hash_ciphertext
        result = gateway.cleanup_authorization(account.session_ciphertext, raw_hash, credentials)
        if result.ok:
            cleaned += 1
            authorization.status = "cleaned"
        else:
            if _is_fresh_reset_forbidden(result.detail or result.failure_type):
                item.next_retry_at = _now() + timedelta(hours=24)
                item.cleanup_status = "waiting"
                item.status = "waiting"
            failures.append(result.detail or result.failure_type)
    item.external_devices_after = max(0, len(external) - cleaned)
    item.cleanup_status = "succeeded" if not failures else "partial_success" if cleaned else "failed"
    snapshot = _snapshot(session, account)
    snapshot.external_authorization_count = item.external_devices_after
    snapshot.last_device_scan_at = _now()
    return failures


def _fresh_session_wait_until(session: Session, account: TgAccount):
    now_value = _now()
    authorization = session.scalar(
        select(TgAccountAuthorizationSnapshot)
        .where(TgAccountAuthorizationSnapshot.account_id == account.id, TgAccountAuthorizationSnapshot.is_current_session.is_(True))
        .order_by(TgAccountAuthorizationSnapshot.date_created.desc(), TgAccountAuthorizationSnapshot.id.desc())
    )
    if not authorization or not authorization.date_created:
        return None
    wait_until = authorization.date_created + timedelta(hours=24)
    if wait_until > now_value:
        return wait_until
    return None


def _is_fresh_reset_forbidden(detail: str | None) -> bool:
    text = (detail or "").upper()
    return "FRESH_RESET_AUTHORISATION_FORBIDDEN" in text or "FRESH_RESET_AUTHORIZATION_FORBIDDEN" in text or ("24" in text and "SESSION" in text)


def _generate_two_fa_password(account: TgAccount, item: TgAccountSecurityBatchItem) -> str:
    token = secrets.token_urlsafe(18)
    return f"TgOps-{account.id}-{item.id}-{token}"


def _execute_avatar_update(session: Session, account: TgAccount, item: TgAccountSecurityBatchItem, credentials, *, overwrite_existing: bool = False) -> object:
    if account.avatar_object_key and not overwrite_existing:
        return SimpleNamespace(ok=True, status="skipped", detail="账号已有头像，未开启覆盖", failure_type="")
    if not item.avatar_source:
        return SimpleNamespace(ok=True, status="skipped", detail="未配置头像来源", failure_type="")
    try:
        avatar_path, object_key = _resolve_avatar_source(session, account, item.avatar_source)
    except ValueError as exc:
        return SimpleNamespace(ok=False, status="failed", detail=str(exc), failure_type="头像来源不可用")
    profile_result = gateway.update_profile(
        account.session_ciphertext,
        first_name=account.tg_first_name or item.generated_first_name or item.generated_display_name or account.display_name,
        last_name=account.tg_last_name or item.generated_last_name,
        bio=account.tg_bio or item.generated_bio,
        avatar_path=str(avatar_path),
        credentials=credentials,
    )
    if not profile_result.ok:
        return SimpleNamespace(ok=False, status="failed", detail=profile_result.detail, failure_type=profile_result.failure_type or "头像上传失败")
    account.avatar_object_key = object_key
    account.profile_sync_status = "已同步"
    account.profile_sync_error = ""
    account.profile_synced_at = _now()
    return SimpleNamespace(ok=True, status="succeeded", detail="头像已更新", failure_type="")


def _resolve_avatar_source(session: Session, account: TgAccount, source: str) -> tuple[Path, str]:
    value = (source or "").strip()
    if not value:
        raise ValueError("头像来源为空")
    if value.startswith("avatar:"):
        value = value.removeprefix("avatar:")
    if value.startswith("material:") or value.isdigit():
        material_id = int(value.removeprefix("material:"))
        material = session.get(Material, material_id)
        if not material or material.tenant_id != account.tenant_id:
            raise ValueError("头像素材不存在或不属于当前租户")
        if material.material_type != "图片":
            raise ValueError("头像素材必须是图片类型")
        if material.source_kind != "upload":
            raise ValueError("头像素材必须是平台上传文件")
        source_path = Path(material.content)
        if not source_path.exists():
            raise ValueError("头像素材文件不存在")
        object_key, avatar_path = save_avatar_bytes(
            tenant_id=account.tenant_id,
            account_id=account.id,
            content_type=material.mime_type or "image/jpeg",
            data=source_path.read_bytes(),
        )
        return avatar_path, object_key
    if value.startswith("avatars/"):
        if not value.startswith(f"avatars/{account.tenant_id}/{account.id}/"):
            raise ValueError("头像对象不属于当前账号")
        avatar_path = object_path(value)
        if not avatar_path.exists():
            raise ValueError("头像对象文件不存在")
        return avatar_path, value
    source_path = Path(value)
    if source_path.is_absolute():
        resolved = source_path.resolve()
        root = media_root().resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError("头像文件必须位于平台媒体目录") from exc
        if not resolved.exists():
            raise ValueError("头像文件不存在")
        suffix = resolved.suffix.lower()
        content_type = "image/png" if suffix == ".png" else "image/webp" if suffix == ".webp" else "image/jpeg"
        object_key, avatar_path = save_avatar_bytes(tenant_id=account.tenant_id, account_id=account.id, content_type=content_type, data=resolved.read_bytes())
        return avatar_path, object_key
    raise ValueError("头像来源格式不支持")


def _validate_avatar_source(session: Session, account: TgAccount, source: str) -> str:
    value = (source or "").strip()
    if not value:
        return "未配置头像来源，将跳过头像设置"
    if value.startswith("avatar:"):
        value = value.removeprefix("avatar:")
    if value.startswith("material:") or value.isdigit():
        try:
            material_id = int(value.removeprefix("material:"))
        except ValueError:
            return "头像素材 ID 格式错误"
        material = session.get(Material, material_id)
        if not material or material.tenant_id != account.tenant_id:
            return "头像素材不存在或不属于当前租户"
        if material.material_type != "图片":
            return "头像素材必须是图片类型"
        if material.source_kind != "upload":
            return "头像素材必须是平台上传文件"
        if not Path(material.content).exists():
            return "头像素材文件不存在"
        return ""
    if value.startswith("avatars/"):
        if not value.startswith(f"avatars/{account.tenant_id}/{account.id}/"):
            return "头像对象不属于当前账号"
        return "" if object_path(value).exists() else "头像对象文件不存在"
    source_path = Path(value)
    if source_path.is_absolute():
        try:
            source_path.resolve().relative_to(media_root().resolve())
        except ValueError:
            return "头像文件必须位于平台媒体目录"
        return "" if source_path.exists() else "头像文件不存在"
    return "头像来源格式不支持"


def _try_usernames(account: TgAccount, item: TgAccountSecurityBatchItem, credentials) -> str:
    for candidate in _json_list(item.username_candidates):
        if not USERNAME_RE.match(candidate):
            continue
        result = gateway.update_username(account.session_ciphertext, candidate, credentials)
        if result.ok:
            return candidate
    return ""


def _item_has_success(item: TgAccountSecurityBatchItem) -> bool:
    return any(status == "succeeded" for status in [item.cleanup_status, item.two_fa_status, item.profile_status, item.username_status, item.avatar_status])


def _refresh_batch_counts(session: Session, batch: TgAccountSecurityBatch) -> None:
    items = list(session.scalars(select(TgAccountSecurityBatchItem).where(TgAccountSecurityBatchItem.batch_id == batch.id)))
    batch.success_count = sum(1 for item in items if item.status == "succeeded")
    batch.failed_count = sum(1 for item in items if item.status in {"failed", "partial_success"})
    batch.skipped_count = sum(1 for item in items if item.status in {"skipped", "manual_required"})
    unfinished = [item for item in items if item.status in {"pending", "running", "waiting"}]
    if unfinished:
        batch.status = "running"
        return
    batch.finished_at = _now()
    if batch.failed_count and batch.success_count:
        batch.status = "partial_success"
    elif batch.failed_count:
        batch.status = "failed"
    else:
        batch.status = "succeeded"


def _valid_actions(action_types: list[str]) -> list[str]:
    values = [action for action in action_types if action in ALL_ACTIONS]
    if not values:
        if action_types:
            raise ValueError(f"unsupported account security actions: {','.join(action_types)}")
        values = ["cleanup_devices", "set_two_fa", "update_profile", "update_username", "update_avatar"]
    return values


def _accounts_for_payload(session: Session, tenant_id: int, account_ids: list[int]) -> list[TgAccount]:
    stmt = select(TgAccount).where(TgAccount.tenant_id == tenant_id, TgAccount.deleted_at.is_(None))
    if account_ids:
        stmt = stmt.where(TgAccount.id.in_(account_ids))
    accounts = list(session.scalars(stmt.order_by(TgAccount.id.asc())))
    if not accounts:
        raise ValueError("no accounts selected")
    return accounts


def _generate_profiles(session: Session, tenant_id: int, accounts: list[TgAccount], strategy) -> list[dict[str, object]]:
    if strategy.generation_mode == "ai_random":
        last_error: Exception | None = None
        try:
            return _generate_profiles_with_ai(session, tenant_id, accounts, strategy)
        except Exception as exc:  # noqa: BLE001 - surfaced as preview warning.
            last_error = exc
        fallback = _generate_profiles_from_local_pool(accounts, strategy)
        for item in fallback:
            item["generation_warning"] = _profile_ai_fallback_warning(last_error)
        return fallback
    return _generate_profiles_from_local_pool(accounts, strategy)


def _profile_ai_fallback_warning(exc: Exception | None) -> str:
    detail = str(exc) if exc else "未知错误"
    if isinstance(exc, TimeoutError) or "timed out" in detail.lower() or "timeout" in detail.lower():
        return f"AI 随机命名本次响应超时，已使用本地随机中文名兜底：{detail}"
    return f"AI 随机命名本次生成失败，已使用本地随机中文名兜底：{detail}"


def _profile_ai_timeout_seconds(account_count: int) -> int:
    return min(PROFILE_AI_MAX_TIMEOUT_SECONDS, max(PROFILE_AI_BASE_TIMEOUT_SECONDS, account_count * 4))


def _apply_preview_override(generated_item: dict[str, object], override) -> dict[str, object]:
    if not override:
        return generated_item
    item = dict(generated_item)
    for key in ["generated_display_name", "generated_first_name", "generated_last_name", "generated_bio", "avatar_source"]:
        value = getattr(override, key, "")
        if value:
            target = key.removeprefix("generated_")
            item[target] = value
    if override.username_candidates:
        item["username_candidates"] = [candidate.strip().lstrip("@") for candidate in override.username_candidates if candidate.strip()]
    return item


def _generate_profiles_with_ai(session: Session, tenant_id: int, accounts: list[TgAccount], strategy) -> list[dict[str, object]]:
    setting = get_tenant_ai_setting(session, tenant_id)
    provider = _profile_ai_provider(session, setting.default_provider_id)
    if not provider:
        raise RuntimeError("没有健康 AI 供应商")
    credentials = ai_provider_credentials(provider)
    if credentials.base_url.startswith("mock://"):
        raise RuntimeError("当前 AI 供应商为 mock")
    count = len(accounts)
    style_prompt = (getattr(strategy, "custom_prompt", "") or "").strip()
    prompt = (
        f"请为 Telegram 运营账号一次性生成 {count} 组随机账号资料。\n"
        f"语言风格：{strategy.language_style}\n"
        f"账号画像：{strategy.persona_style}\n"
        f"性别倾向：{strategy.gender_bias}\n"
        f"年龄风格：{strategy.age_style}\n"
        f"username 前缀提示：{strategy.username_prefix_hint or '无'}\n"
        f"每个账号 username 候选数量：{strategy.username_max_attempts}\n"
        f"禁用词：{', '.join(strategy.forbidden_words) or '无'}\n"
        f"用户补充命名风格：{style_prompt or '像真实 TG 普通用户的随手昵称，不要正式姓名'}\n"
        "昵称要求：display_name 要像真实用户网名/昵称，随机、生活化、有网感，可以是中文短语、食物、梗名、心情或轻微拟人化；"
        "例如：锅巴洋芋、蕉太狼、早睡失败、小熊便利店、不吃香菜、月亮打烊。不要批量生成“张雨晴、李浩然、王思远”这类正式姓名，避免公司/客服/营销号口吻，避免规律序号。\n"
        "姓名字段要求：first_name 可以直接等于 display_name；last_name 可以为空，不要强行拆成中文姓氏和名字。\n"
        "username 要求：只能包含英文字母、数字、下划线，且以字母开头，长度 5-32；可以根据昵称意译或拼音化生成，不要包含中文。\n"
        '只输出 JSON：{"items":[{"display_name":"锅巴洋芋","first_name":"锅巴洋芋","last_name":"","bio":"看到有意思的会回两句","username_candidates":["guoba_yangyu","potato_crisp","yangyu_daily"]}]}'
    )
    raw, _usage = ai_gateway._post_openai_compatible(  # noqa: SLF001 - reuse the repo's provider adapter for JSON generation.
        credentials,
        prompt,
        setting.temperature,
        max(setting.max_tokens, count * 256, 1200),
        system_prompt="你是 Telegram 账号资料生成器。生成真实、随机、生活化的 TG 用户昵称，不生成正式姓名或营销号名称；只输出紧凑合法 JSON，不要解释。",
        response_format_json=True,
        reasoning_retry_max_tokens=max(setting.max_tokens, count * 512, 2048),
        timeout=_profile_ai_timeout_seconds(count),
    )
    return _parse_ai_profile_items(raw, count, strategy)


def _profile_ai_provider(session: Session, provider_id: int | None) -> AiProvider | None:
    if provider_id:
        provider = session.get(AiProvider, provider_id)
        if provider and provider.is_active and provider.health_status == AiProviderHealthStatus.HEALTHY.value:
            return provider
    return session.scalar(
        select(AiProvider)
        .where(AiProvider.is_active.is_(True), AiProvider.health_status == AiProviderHealthStatus.HEALTHY.value)
        .order_by(AiProvider.id.asc())
    )


def _parse_ai_profile_items(raw: str, count: int, strategy) -> list[dict[str, object]]:
    clean = raw.strip()
    if clean.startswith("```"):
        clean = clean.strip("`").removeprefix("json").strip()
    data = json.loads(clean)
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise RuntimeError("AI 输出不是 items 数组")
    results: list[dict[str, object]] = []
    used_names: set[str] = set()
    used_usernames: set[str] = set()
    forbidden = {word.strip() for word in strategy.forbidden_words if word.strip()}
    for raw_item in items:
        if not isinstance(raw_item, dict):
            continue
        display_name = str(raw_item.get("display_name") or "").strip()[:80]
        first_name = str(raw_item.get("first_name") or display_name).strip()[:80]
        last_name = str(raw_item.get("last_name") or "").strip()[:80]
        bio = str(raw_item.get("bio") or "").strip()[:160] if strategy.bio_enabled else ""
        candidates_raw = raw_item.get("username_candidates") or []
        candidates = [str(candidate).strip().lstrip("@") for candidate in candidates_raw if isinstance(candidate, str)]
        candidates = [candidate for candidate in candidates if USERNAME_RE.match(candidate)]
        candidates = [candidate for candidate in candidates if candidate.lower() not in used_usernames]
        if not display_name or display_name in used_names:
            continue
        if any(word and (word in display_name or word in bio) for word in forbidden):
            continue
        if strategy.username_enabled and not candidates:
            continue
        used_names.add(display_name)
        used_usernames.update(candidate.lower() for candidate in candidates)
        results.append(
            {
                "display_name": display_name,
                "first_name": first_name,
                "last_name": last_name,
                "bio": bio,
                "username_candidates": candidates[: strategy.username_max_attempts] if strategy.username_enabled else [],
            }
        )
        if len(results) >= count:
            break
    if len(results) < count:
        raise RuntimeError(f"AI 生成资料不足：{len(results)}/{count}")
    return results


def _generate_profiles_from_local_pool(accounts: list[TgAccount], strategy) -> list[dict[str, object]]:
    forbidden = {word.strip() for word in strategy.forbidden_words if word.strip()}
    results: list[dict[str, object]] = []
    for index, account in enumerate(accounts):
        display_name, suffix, bio = AI_NAME_POOL[(account.id + index) % len(AI_NAME_POOL)]
        if suffix:
            display_name = f"{display_name}{suffix}"
        if any(word in display_name for word in forbidden):
            display_name = f"用户{account.id}"
        first_name = display_name
        last_name = ""
        username_base = (strategy.username_prefix_hint or _romanize_name(display_name) or f"user{account.id}").lower()
        username_base = re.sub(r"[^a-z0-9_]", "", username_base)[:20] or f"user{account.id}"
        candidates = [f"{username_base}_{account.id + offset:03d}" for offset in range(strategy.username_max_attempts)]
        results.append(
            {
                "display_name": display_name,
                "first_name": first_name,
                "last_name": last_name,
                "bio": bio if strategy.bio_enabled else "",
                "username_candidates": candidates if strategy.username_enabled else [],
            }
        )
    return results


def _romanize_name(value: str) -> str:
    # Local deterministic fallback for preview tests; live AI output can provide richer candidates.
    return "tguser"


def _can_replace_display_name(display_name: str | None) -> bool:
    value = (display_name or "").strip()
    return value in GENERIC_DISPLAY_NAMES or bool(SYSTEM_DISPLAY_NAME_RE.match(value))


def _avatar_source(index: int, strategy) -> str:
    if strategy.avatar_sources:
        return strategy.avatar_sources[index % len(strategy.avatar_sources)]
    if strategy.material_group_id:
        return f"material_group:{strategy.material_group_id}:{index + 1}"
    return ""


__all__ = [
    "account_security_batch_detail",
    "account_security_detail",
    "account_security_summary",
    "cancel_account_security_batch",
    "create_account_security_batch",
    "drain_account_security_batches",
    "list_account_security_batches",
    "precheck_account_security_batch",
    "refresh_account_security",
    "retry_account_security_batch",
]
