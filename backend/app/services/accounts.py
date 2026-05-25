from __future__ import annotations

import json
import logging
import random
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.integrations.telegram import ContactSnapshot, VerificationCodeSnapshot
from app.models import (
    AccountPool,
    AccountStatus,
    FailureType,
    GroupAuthStatus,
    MessageTask,
    MessageTaskAttempt,
    TaskStatus,
    TgAccount,
    TgAccountProfileSyncRecord,
    TgAccountSyncRecord,
    TgContact,
    TgGroup,
    TgGroupAccount,
    TgLoginFlow,
    TgVerificationCode,
)
from app.schemas import TgAccountCreate, TgAccountProfileUpdate
from app.security import encrypt_secret, encrypt_session
from app.storage import object_path, preview_url, save_avatar_bytes

from ._common import _is_expired, _now, audit, gateway, get_account_phone, mask_phone, require_tenant
from .developer_apps import credentials_for_account, first_assignable_developer_app
from .tenants import ensure_account_quota_available
from .verification import list_verification_tasks, create_verification_task
from .account_pools import account_pool_snapshot, ensure_default_account_pool, seed_account_pools

ACCOUNT_SYNC_INTERVAL = timedelta(hours=1)
ACCOUNT_SYNC_STAGGER_STEP = timedelta(seconds=3)
GENERIC_ACCOUNT_DISPLAY_NAMES = {"", "托管账号", "新托管账号", "未命名账号"}
ACCOUNT_SYNC_STALE_AFTER = timedelta(minutes=30)
ACCOUNT_SYNC_QUEUE_STALE_AFTER = timedelta(hours=6)
PROFILE_SYNC_STALE_AFTER = timedelta(minutes=30)
PROFILE_SYNC_QUEUE_STALE_AFTER = timedelta(hours=6)
ACCOUNT_AUTO_SYNC_SKIP_STATUSES = {
    AccountStatus.PENDING_LOGIN.value,
    AccountStatus.WAITING_CODE.value,
    AccountStatus.WAITING_QR.value,
    AccountStatus.WAITING_2FA.value,
    AccountStatus.NEED_RELOGIN.value,
    AccountStatus.SESSION_EXPIRED.value,
    AccountStatus.DISABLED.value,
}
LOGIN_START_FAILURE_MESSAGE = "TG 登录启动失败，请检查开发者应用、手机号、代理或 Telegram 限制后重试"

logger = logging.getLogger(__name__)


class LoginStartFailure(Exception):
    def __init__(self, *, detail: dict[str, object]) -> None:
        super().__init__(str(detail.get("message") or LOGIN_START_FAILURE_MESSAGE))
        self.detail = detail

__all__ = [
    "LoginStartFailure",
    "account_contacts",
    "account_detail",
    "account_groups",
    "account_message_records",
    "account_profile_snapshot",
    "check_qr_login",
    "filter_accounts",
    "create_account",
    "drain_account_sync_records",
    "drain_profile_sync_records",
    "find_account_contact",
    "health_check_account",
    "list_account_sync_records",
    "list_login_flows",
    "list_profile_sync_records",
    "list_verification_codes",
    "poll_account_verification_codes",
    "process_account_sync_record",
    "process_profile_sync_record",
    "queue_account_sync_now",
    "queue_account_sync_records",
    "retry_account_profile_sync",
    "run_account_sync_now",
    "start_login",
    "soft_delete_account",
    "sync_account_contacts",
    "sync_remote_profile",
    "sync_groups",
    "update_account_profile",
    "upload_account_avatar",
    "verify_login",
]


def create_account(session: Session, payload: TgAccountCreate, actor: str = "普通用户") -> TgAccount:
    require_tenant(session, payload.tenant_id)
    ensure_account_quota_available(session, payload.tenant_id)
    if not first_assignable_developer_app(session):
        raise ValueError("请先在开发者应用中配置可用的 Telegram api_id/api_hash，再新增 TG 账号")
    data = payload.model_dump(exclude={"phone_number"})
    pool_id = data.get("pool_id")
    if pool_id:
        pool = session.get(AccountPool, pool_id)
        if not pool or pool.tenant_id != payload.tenant_id:
            raise ValueError("account pool not found")
    else:
        data["pool_id"] = ensure_default_account_pool(session, payload.tenant_id).id
    phone_number = (payload.phone_number or "").strip()
    if phone_number:
        data["phone_ciphertext"] = encrypt_secret(phone_number)
        data["phone_masked"] = mask_phone(phone_number)
    elif not data.get("phone_masked"):
        raise ValueError("phone_number or phone_masked is required")
    existing = session.scalar(
        select(TgAccount).where(
            TgAccount.tenant_id == payload.tenant_id,
            TgAccount.phone_masked == data["phone_masked"],
            TgAccount.deleted_at.is_(None),
        )
    )
    if existing:
        raise ValueError("同租户下该手机号已存在可用账号，请先移除旧账号或更换手机号")
    data["display_name"] = _account_display_name(session, payload.tenant_id, data.get("display_name") or "", phone_number or data.get("phone_masked") or "")
    _sync_account_id_sequence(session)
    account = TgAccount(**data)
    session.add(account)
    session.flush()
    audit(session, tenant_id=account.tenant_id, actor=actor, action="添加TG账号", target_type="tg_account", target_id=str(account.id))
    session.commit()
    session.refresh(account)
    return account


def _sync_account_id_sequence(session: Session) -> None:
    if not session.bind or session.bind.dialect.name != "postgresql":
        return
    session.execute(
        text(
            """
            SELECT setval(
                pg_get_serial_sequence('tg_accounts', 'id'),
                GREATEST(COALESCE((SELECT MAX(id) FROM tg_accounts), 0), 1),
                COALESCE((SELECT MAX(id) FROM tg_accounts), 0) > 0
            )
            """
        )
    )


def _account_display_name(session: Session, tenant_id: int, display_name: str, phone_value: str) -> str:
    cleaned = (display_name or "").strip()
    if cleaned and cleaned not in GENERIC_ACCOUNT_DISPLAY_NAMES:
        return cleaned
    digits = "".join(char for char in (phone_value or "") if char.isdigit())
    tail = digits[-4:] if len(digits) >= 4 else "0000"
    now_value = _now()
    day_start = now_value.replace(hour=0, minute=0, second=0, microsecond=0)
    imported_today = session.scalar(
        select(func.count(TgAccount.id)).where(
            TgAccount.tenant_id == tenant_id,
            TgAccount.deleted_at.is_(None),
            TgAccount.created_at >= day_start,
        )
    ) or 0
    return f"导入{now_value:%m%d}-{tail}-{imported_today + 1:03d}"


def soft_delete_account(session: Session, account_id: int, actor: str = "普通用户", reason: str = "用户移除") -> TgAccount:
    account = session.get(TgAccount, account_id)
    if not account:
        raise ValueError("account not found")
    if account.deleted_at is None:
        account.deleted_at = _now()
        account.deleted_by = actor
        account.delete_reason = reason
        account.status = AccountStatus.DISABLED.value
        audit(
            session,
            tenant_id=account.tenant_id,
            actor=actor,
            action="移除TG账号",
            target_type="tg_account",
            target_id=str(account.id),
            detail=reason,
        )
    for link in session.scalars(select(TgGroupAccount).where(TgGroupAccount.account_id == account.id)):
        link.can_send = False
        link.is_listener = False
    for record in session.scalars(
        select(TgAccountSyncRecord).where(
            TgAccountSyncRecord.account_id == account.id,
            TgAccountSyncRecord.status.in_(["排队中", "同步中"]),
        )
    ):
        record.status = "已取消"
        record.failure_type = "账号已删除"
        record.failure_detail = reason
        record.finished_at = record.finished_at or _now()
    for record in session.scalars(
        select(TgAccountProfileSyncRecord).where(
            TgAccountProfileSyncRecord.account_id == account.id,
            TgAccountProfileSyncRecord.status.in_(["排队中", "同步中"]),
        )
    ):
        record.status = "已取消"
        record.failure_type = "账号已删除"
        record.failure_detail = reason
        record.synced_at = record.synced_at or _now()
    for flow in session.scalars(
        select(TgLoginFlow).where(
            TgLoginFlow.account_id == account.id,
            TgLoginFlow.status.in_([
                AccountStatus.PENDING_LOGIN.value,
                AccountStatus.WAITING_CODE.value,
                AccountStatus.WAITING_QR.value,
                AccountStatus.WAITING_2FA.value,
            ]),
        )
    ):
        flow.status = "已取消"
        flow.code_preview = None
    session.commit()
    session.refresh(account)
    return account


def _ensure_account_available(account: TgAccount | None) -> TgAccount:
    if not account or account.deleted_at is not None:
        raise ValueError("account not found")
    return account


def account_profile_snapshot(account: TgAccount) -> dict[str, str]:
    return {
        "display_name": account.display_name,
        "tg_first_name": account.tg_first_name,
        "tg_last_name": account.tg_last_name,
        "tg_bio": account.tg_bio,
        "avatar_object_key": account.avatar_object_key,
    }


def list_profile_sync_records(session: Session, account_id: int, limit: int = 20) -> list[TgAccountProfileSyncRecord]:
    account = session.get(TgAccount, account_id)
    if not account:
        raise ValueError("account not found")
    return list(
        session.scalars(
            select(TgAccountProfileSyncRecord)
            .where(TgAccountProfileSyncRecord.tenant_id == account.tenant_id, TgAccountProfileSyncRecord.account_id == account.id)
            .order_by(TgAccountProfileSyncRecord.id.desc())
            .limit(limit)
        )
    )


def _mark_stale_profile_sync_records(session: Session, account_id: int | None = None) -> int:
    now_value = _now()
    count = 0
    stmt = select(TgAccountProfileSyncRecord).where(TgAccountProfileSyncRecord.status.in_(["排队中", "同步中"]))
    if account_id is not None:
        stmt = stmt.where(TgAccountProfileSyncRecord.account_id == account_id)
    for record in session.scalars(stmt):
        age = now_value - record.created_at
        if record.status == "同步中" and age < PROFILE_SYNC_STALE_AFTER:
            continue
        if record.status == "排队中" and age < PROFILE_SYNC_QUEUE_STALE_AFTER:
            continue
        record.status = "失败"
        record.failure_type = "资料同步超时"
        record.failure_detail = "资料同步任务长时间未完成，已停止等待，请重新提交同步。"
        record.synced_at = now_value
        account = session.get(TgAccount, record.account_id)
        if account and account.deleted_at is None:
            latest = session.scalar(
                select(TgAccountProfileSyncRecord)
                .where(TgAccountProfileSyncRecord.account_id == account.id)
                .order_by(TgAccountProfileSyncRecord.id.desc())
            )
            if not latest or latest.id == record.id:
                account.profile_sync_status = "失败"
                account.profile_sync_error = record.failure_detail
        count += 1
    return count


def upload_account_avatar(session: Session, account_id: int, filename: str, content_type: str, data: bytes, actor: str) -> dict:
    account = _ensure_account_available(session.get(TgAccount, account_id))
    settings = get_settings()
    if not data:
        raise ValueError("avatar file is empty")
    if len(data) > settings.avatar_max_bytes:
        raise ValueError(f"avatar file is too large, max {settings.avatar_max_bytes} bytes")
    if content_type not in settings.avatar_allowed_types:
        raise ValueError("unsupported avatar content type")
    object_key, _ = save_avatar_bytes(tenant_id=account.tenant_id, account_id=account.id, content_type=content_type, data=data)
    audit(
        session,
        tenant_id=account.tenant_id,
        actor=actor,
        action="上传TG账号头像",
        target_type="tg_account",
        target_id=str(account.id),
        detail=f"{filename}; {content_type}; {len(data)} bytes",
    )
    session.commit()
    return {"object_key": object_key, "preview_url": preview_url(object_key), "content_type": content_type, "size": len(data)}


def update_account_profile(session: Session, account_id: int, payload: TgAccountProfileUpdate, actor: str) -> TgAccount:
    account = _ensure_account_available(session.get(TgAccount, account_id))
    if not payload.display_name.strip():
        raise ValueError("display_name is required")
    if payload.avatar_object_key and not payload.avatar_object_key.startswith(f"avatars/{account.tenant_id}/{account.id}/"):
        raise ValueError("avatar does not belong to this account")

    before = account_profile_snapshot(account)
    account.display_name = payload.display_name.strip()
    account.tg_first_name = payload.tg_first_name.strip()
    account.tg_last_name = payload.tg_last_name.strip()
    account.tg_bio = payload.tg_bio.strip()
    account.avatar_object_key = payload.avatar_object_key.strip()
    account.profile_sync_status = "排队中"
    account.profile_sync_error = ""

    record = TgAccountProfileSyncRecord(
        tenant_id=account.tenant_id,
        account_id=account.id,
        actor=actor,
        before_snapshot=json.dumps(before, ensure_ascii=False),
        after_snapshot=json.dumps(account_profile_snapshot(account), ensure_ascii=False),
        avatar_object_key=account.avatar_object_key,
        status="排队中",
    )
    session.add(record)
    session.flush()
    audit(session, tenant_id=account.tenant_id, actor=actor, action="保存TG账号资料", target_type="tg_account", target_id=str(account.id), detail=f"profile_sync_record={record.id}")
    session.commit()
    session.refresh(account)
    return account


def retry_account_profile_sync(session: Session, account_id: int, actor: str) -> TgAccountProfileSyncRecord:
    account = _ensure_account_available(session.get(TgAccount, account_id))
    latest = session.scalar(
        select(TgAccountProfileSyncRecord)
        .where(TgAccountProfileSyncRecord.tenant_id == account.tenant_id, TgAccountProfileSyncRecord.account_id == account.id)
        .order_by(TgAccountProfileSyncRecord.id.desc())
    )
    if not latest:
        latest = TgAccountProfileSyncRecord(
            tenant_id=account.tenant_id,
            account_id=account.id,
            actor=actor,
            before_snapshot=json.dumps(account_profile_snapshot(account), ensure_ascii=False),
            after_snapshot=json.dumps(account_profile_snapshot(account), ensure_ascii=False),
            avatar_object_key=account.avatar_object_key,
            status="排队中",
        )
        session.add(latest)
    else:
        latest.status = "排队中"
        latest.failure_type = ""
        latest.failure_detail = ""
        latest.remote_detail = ""
        latest.actor = actor
    account.profile_sync_status = "排队中"
    account.profile_sync_error = ""
    audit(session, tenant_id=account.tenant_id, actor=actor, action="重试TG账号资料同步", target_type="tg_account", target_id=str(account.id))
    session.commit()
    session.refresh(latest)
    return latest


def process_profile_sync_record(session: Session, record_id: int) -> TgAccountProfileSyncRecord:
    record = session.get(TgAccountProfileSyncRecord, record_id)
    if not record:
        raise ValueError("profile sync record not found")
    account = session.get(TgAccount, record.account_id)
    if not account or account.tenant_id != record.tenant_id or account.deleted_at is not None:
        raise ValueError("account not found")
    record.status = "同步中"
    account.profile_sync_status = "同步中"
    session.commit()

    avatar_path = str(object_path(account.avatar_object_key)) if account.avatar_object_key and object_path(account.avatar_object_key).exists() else None
    try:
        credentials = credentials_for_account(session, account)
        result = gateway.update_profile(
            account.session_ciphertext,
            first_name=account.tg_first_name or account.display_name,
            last_name=account.tg_last_name,
            bio=account.tg_bio,
            avatar_path=avatar_path,
            credentials=credentials,
        )
    except Exception as exc:  # noqa: BLE001 - operator-facing sync detail.
        result = type("_ProfileResult", (), {"ok": False, "detail": str(exc), "failure_type": "账号不可用"})()

    if result.ok:
        record.status = "已同步"
        record.remote_detail = result.detail
        record.failure_type = ""
        record.failure_detail = ""
        record.synced_at = _now()
        account.profile_sync_status = "已同步"
        account.profile_sync_error = ""
        account.profile_synced_at = record.synced_at
        audit(session, tenant_id=account.tenant_id, actor="tg-worker", action="同步TG账号资料成功", target_type="tg_account", target_id=str(account.id), detail=f"profile_sync_record={record.id}")
    else:
        record.status = "失败"
        record.failure_type = result.failure_type or "未知错误"
        record.failure_detail = result.detail or ""
        account.profile_sync_status = "失败"
        account.profile_sync_error = record.failure_detail
        audit(session, tenant_id=account.tenant_id, actor="tg-worker", action="同步TG账号资料失败", target_type="tg_account", target_id=str(account.id), detail=record.failure_detail)
    session.commit()
    session.refresh(record)
    return record


def drain_profile_sync_records(session_factory, limit: int = 20) -> int:
    count = 0
    with session_factory() as session:
        _mark_stale_profile_sync_records(session)
        session.commit()
        record_ids = list(
            session.scalars(
                select(TgAccountProfileSyncRecord.id)
                .where(TgAccountProfileSyncRecord.status == "排队中")
                .order_by(TgAccountProfileSyncRecord.id.asc())
                .limit(limit)
            )
        )
    for record_id in record_ids:
        with session_factory() as session:
            process_profile_sync_record(session, record_id)
            count += 1
    return count


def queue_account_sync_records(
    session: Session,
    account: TgAccount,
    *,
    trigger_source: str,
    sync_types: list[str] | None = None,
    scheduled_at: datetime | None = None,
) -> list[TgAccountSyncRecord]:
    sync_types = sync_types or ["profile_pull", "health", "groups", "targets", "contacts", "codes"]
    records: list[TgAccountSyncRecord] = []
    now_value = _now()
    for sync_type in sync_types:
        existing = session.scalar(
            select(TgAccountSyncRecord)
            .where(
                TgAccountSyncRecord.tenant_id == account.tenant_id,
                TgAccountSyncRecord.account_id == account.id,
                TgAccountSyncRecord.sync_type == sync_type,
                TgAccountSyncRecord.status == "排队中",
            )
            .order_by(TgAccountSyncRecord.id.desc())
        )
        if existing:
            scheduled_at = existing.scheduled_at or existing.created_at
            if scheduled_at > now_value - ACCOUNT_SYNC_QUEUE_STALE_AFTER:
                records.append(existing)
                continue
            existing.status = "失败"
            existing.failure_type = "排队超时"
            existing.failure_detail = "同步任务长时间未被消费，已重新创建新任务。"
            existing.finished_at = now_value
        record = TgAccountSyncRecord(
            tenant_id=account.tenant_id,
            account_id=account.id,
            sync_type=sync_type,
            trigger_source=trigger_source,
            status="排队中",
            scheduled_at=scheduled_at or _now(),
        )
        session.add(record)
        records.append(record)
    audit(session, tenant_id=account.tenant_id, actor="system", action="创建账号同步任务", target_type="tg_account", target_id=str(account.id), detail=",".join(sync_types))
    return records


def list_account_sync_records(session: Session, account_id: int, limit: int = 30) -> list[TgAccountSyncRecord]:
    account = session.get(TgAccount, account_id)
    if not account:
        raise ValueError("account not found")
    return list(
        session.scalars(
            select(TgAccountSyncRecord)
            .where(TgAccountSyncRecord.tenant_id == account.tenant_id, TgAccountSyncRecord.account_id == account.id)
            .order_by(TgAccountSyncRecord.id.desc())
            .limit(limit)
        )
    )


def run_account_sync_now(session: Session, account_id: int, actor: str, trigger_source: str = "manual", sync_types: list[str] | None = None) -> list[TgAccountSyncRecord]:
    account = _ensure_account_available(session.get(TgAccount, account_id))
    records = queue_account_sync_records(session, account, trigger_source=trigger_source, sync_types=sync_types)
    audit(session, tenant_id=account.tenant_id, actor=actor, action="同步账号数据", target_type="tg_account", target_id=str(account.id), detail=f"trigger={trigger_source}")
    session.commit()
    record_ids = [record.id for record in records]
    for record_id in record_ids:
        process_account_sync_record(session, record_id)
    session.refresh(account)
    return list_account_sync_records(session, account.id, limit=len(record_ids) + 5)


def queue_account_sync_now(session: Session, account_id: int, actor: str, sync_types: list[str] | None = None) -> list[TgAccountSyncRecord]:
    return run_account_sync_now(session, account_id, actor, trigger_source="manual", sync_types=sync_types)


def process_account_sync_record(session: Session, record_id: int) -> TgAccountSyncRecord:
    record = session.get(TgAccountSyncRecord, record_id)
    if not record:
        raise ValueError("sync record not found")
    if record.status != "排队中":
        return record
    account = session.get(TgAccount, record.account_id)
    record.status = "同步中"
    record.started_at = _now()
    session.commit()
    result_count = 0
    try:
        if not account or account.deleted_at is not None:
            raise ValueError("account not found")
        if record.sync_type == "groups":
            result_count = len(sync_groups(session, account.id, actor="tg-worker"))
        elif record.sync_type == "targets":
            from .operations import sync_account_targets

            result_count = len(sync_account_targets(session, account.id, actor="tg-worker"))
        elif record.sync_type == "contacts":
            result_count = len(sync_account_contacts(session, account.id, "tg-worker"))
        elif record.sync_type == "codes":
            result_count = len(poll_account_verification_codes(session, account.id, "tg-worker", "系统同步账号数据"))
        elif record.sync_type == "health":
            health_check_account(session, account.id)
            result_count = 1
        elif record.sync_type == "profile_pull":
            sync_remote_profile(session, account.id, "tg-worker")
            result_count = 1
        else:
            raise ValueError(f"unsupported sync type: {record.sync_type}")
        record = session.get(TgAccountSyncRecord, record_id)
        record.status = "已同步"
        record.result_count = result_count
        record.failure_type = ""
        record.failure_detail = ""
    except Exception as exc:  # noqa: BLE001 - operator-facing sync detail.
        record = session.get(TgAccountSyncRecord, record_id)
        record.status = "失败"
        record.failure_type = "同步失败"
        record.failure_detail = str(exc)
    record.finished_at = _now()
    audit(session, tenant_id=record.tenant_id, actor="tg-worker", action="执行账号同步任务", target_type="account_sync_record", target_id=str(record.id), detail=f"{record.sync_type}:{record.status}:{record.result_count}")
    session.commit()
    session.refresh(record)
    return record


def _mark_stale_account_sync_records(session: Session, account_id: int | None = None) -> int:
    now_value = _now()
    count = 0
    stmt = select(TgAccountSyncRecord).where(TgAccountSyncRecord.status == "同步中")
    if account_id is not None:
        stmt = stmt.where(TgAccountSyncRecord.account_id == account_id)
    for record in session.scalars(stmt):
        started_at = record.started_at or record.created_at
        if now_value - started_at < ACCOUNT_SYNC_STALE_AFTER:
            continue
        record.status = "失败"
        record.failure_type = "同步超时"
        record.failure_detail = "同步任务长时间未完成，已停止等待，下一轮会重新同步。"
        record.finished_at = now_value
        count += 1
    return count


def drain_account_sync_records(session_factory, limit: int = 20) -> int:
    count = 0
    with session_factory() as session:
        _mark_stale_account_sync_records(session)
        cutoff = _now() - ACCOUNT_SYNC_INTERVAL
        syncable_accounts = list(
            session.scalars(
                select(TgAccount)
                .where(
                    TgAccount.status.not_in(ACCOUNT_AUTO_SYNC_SKIP_STATUSES),
                    TgAccount.deleted_at.is_(None),
                    TgAccount.session_ciphertext.is_not(None),
                    TgAccount.session_ciphertext != "",
                )
                .order_by(TgAccount.id.asc())
            )
        )
        next_scheduled_at = _now()
        stagger_index = 0
        for account in syncable_accounts:
            latest = session.scalar(
                select(TgAccountSyncRecord)
                .where(TgAccountSyncRecord.account_id == account.id)
                .order_by(TgAccountSyncRecord.created_at.desc())
                .limit(1)
            )
            if not latest or latest.created_at <= cutoff:
                queue_account_sync_records(
                    session,
                    account,
                    trigger_source="scheduled",
                    sync_types=["health"],
                    scheduled_at=next_scheduled_at + (ACCOUNT_SYNC_STAGGER_STEP * stagger_index),
                )
                stagger_index += 1
        session.commit()
        record_ids = list(
            session.scalars(
                select(TgAccountSyncRecord.id)
                .where(TgAccountSyncRecord.status == "排队中", TgAccountSyncRecord.scheduled_at <= _now())
                .order_by(TgAccountSyncRecord.scheduled_at.asc(), TgAccountSyncRecord.created_at.asc(), TgAccountSyncRecord.id.asc())
                .limit(limit)
            )
        )
    for record_id in record_ids:
        with session_factory() as session:
            process_account_sync_record(session, record_id)
            count += 1
    return count


def start_login(session: Session, account_id: int, method: str, actor: str = "普通用户", force: bool = False) -> TgLoginFlow:
    account = _ensure_account_available(session.get(TgAccount, account_id))
    if account.status == AccountStatus.ACTIVE.value and not force:
        raise ValueError("account already online; use force to restart login")
    trace_id = uuid4().hex
    try:
        credentials = credentials_for_account(session, account, assign_if_missing=True)
        phone = get_account_phone(account)
        challenge = gateway.start_login(method, account_id=account.id, phone=phone, credentials=credentials)
    except Exception as exc:
        _record_login_start_failure(session, account, method, actor, exc, trace_id)
        logger.exception(
            "tg login start failed account_id=%s developer_app_id=%s trace_id=%s",
            account.id,
            account.developer_app_id,
            trace_id,
        )
        raise LoginStartFailure(detail=_login_start_failure_detail(account, exc, trace_id)) from exc
    account.status = challenge.status
    flow = TgLoginFlow(
        tenant_id=account.tenant_id,
        account_id=account.id,
        method=method,
        status=challenge.status,
        code_preview=challenge.code_preview,
        code_expires_at=challenge.code_expires_at,
        qr_payload=challenge.qr_payload,
    )
    session.add(flow)
    if challenge.code_preview:
        session.add(
            TgVerificationCode(
                tenant_id=account.tenant_id,
                account_id=account.id,
                source="login_flow",
                code_preview=challenge.code_preview,
                expires_at=challenge.code_expires_at,
                raw_hint="平台发起登录验证码",
            )
        )
    audit(session, tenant_id=account.tenant_id, actor=actor, action="开始TG登录", target_type="tg_account", target_id=str(account.id), detail=f"method={method}; developer_app_id={account.developer_app_id}")
    session.commit()
    session.refresh(flow)
    return flow


def _record_login_start_failure(session: Session, account: TgAccount, method: str, actor: str, exc: Exception, trace_id: str) -> None:
    failure_type = type(exc).__name__
    failure_detail = str(exc) or failure_type
    account.status = AccountStatus.ERROR.value
    flow = TgLoginFlow(
        tenant_id=account.tenant_id,
        account_id=account.id,
        method=method,
        status="登录失败",
        failure_type=failure_type,
        failure_detail=failure_detail,
        trace_id=trace_id,
    )
    session.add(flow)
    audit(
        session,
        tenant_id=account.tenant_id,
        actor=actor,
        action="开始TG登录失败",
        target_type="tg_account",
        target_id=str(account.id),
        detail=f"method={method}; developer_app_id={account.developer_app_id}; trace_id={trace_id}; failure_type={failure_type}; failure_detail={failure_detail}",
    )
    session.commit()


def _login_start_failure_detail(account: TgAccount, exc: Exception, trace_id: str) -> dict[str, object]:
    return {
        "message": LOGIN_START_FAILURE_MESSAGE,
        "account_id": account.id,
        "trace_id": trace_id,
        "failure_type": type(exc).__name__,
        "failure_detail": str(exc) or type(exc).__name__,
    }


def list_login_flows(session: Session, account_id: int) -> list[TgLoginFlow]:
    account = _ensure_account_available(session.get(TgAccount, account_id))
    flows = list(
        session.scalars(
            select(TgLoginFlow)
            .where(TgLoginFlow.account_id == account_id, TgLoginFlow.tenant_id == account.tenant_id)
            .order_by(TgLoginFlow.id.desc())
        )
    )
    changed = False
    for flow in flows:
        if flow.code_preview and _is_expired(flow.code_expires_at):
            flow.code_preview = None
            flow.status = "已过期"
            changed = True
    if changed:
        audit(session, tenant_id=account.tenant_id, actor="system", action="隐藏过期验证码", target_type="tg_account", target_id=str(account.id))
        session.commit()
    return flows


def verify_login(session: Session, account_id: int, code: str | None, password_2fa: str | None, actor: str = "普通用户") -> TgAccount:
    account = _ensure_account_available(session.get(TgAccount, account_id))
    if account.status == AccountStatus.ACTIVE.value and account.session_ciphertext:
        return account

    latest_flow = session.scalar(
        select(TgLoginFlow)
        .where(TgLoginFlow.account_id == account_id)
        .order_by(TgLoginFlow.id.desc())
        .limit(1)
    )
    if latest_flow and latest_flow.code_preview and _is_expired(latest_flow.code_expires_at) and not password_2fa:
        latest_flow.code_preview = None
        latest_flow.status = "已过期"
        if not code:
            account.status = AccountStatus.ERROR.value
            audit(session, tenant_id=account.tenant_id, actor=actor, action="验证TG登录失败", target_type="tg_account", target_id=str(account.id), detail="code expired")
            session.commit()
            session.refresh(account)
            return account

    credentials = credentials_for_account(session, account)
    status, raw_session = gateway.finish_login(code, password_2fa, account_id=account.id, phone=get_account_phone(account), credentials=credentials)
    account.status = status
    should_sync = False
    if raw_session:
        account.session_ciphertext = encrypt_session(raw_session)
        account.last_active_at = _now()
        account.health_score = max(account.health_score, 90)
        if latest_flow:
            latest_flow.code_preview = None
            latest_flow.status = status
        if status == AccountStatus.ACTIVE.value:
            should_sync = True

    audit(session, tenant_id=account.tenant_id, actor=actor, action="验证TG登录", target_type="tg_account", target_id=str(account.id), detail=f"status={status}")
    session.commit()
    if should_sync:
        run_account_sync_now(session, account.id, actor, trigger_source="login")
    session.refresh(account)
    return account


def check_qr_login(session: Session, account_id: int, actor: str = "普通用户") -> TgAccount:
    account = _ensure_account_available(session.get(TgAccount, account_id))
    latest_flow = session.scalar(
        select(TgLoginFlow)
        .where(TgLoginFlow.account_id == account_id, TgLoginFlow.method == "qr")
        .order_by(TgLoginFlow.id.desc())
        .limit(1)
    )
    if not latest_flow:
        raise ValueError("qr login flow not found")
    if latest_flow.status == AccountStatus.ACTIVE.value:
        return account
    credentials = credentials_for_account(session, account)
    status, raw_session = gateway.finish_login("qr-confirmed", None, account_id=account.id, phone=get_account_phone(account), credentials=credentials)
    account.status = status
    should_sync = False
    if raw_session:
        account.session_ciphertext = encrypt_session(raw_session)
        account.last_active_at = _now()
        account.health_score = max(account.health_score, 90)
        if status == AccountStatus.ACTIVE.value:
            should_sync = True
    latest_flow.status = status
    audit(session, tenant_id=account.tenant_id, actor=actor, action="检查QR登录", target_type="tg_account", target_id=str(account.id), detail=f"status={status}")
    session.commit()
    if should_sync:
        run_account_sync_now(session, account.id, actor, trigger_source="login")
    session.refresh(account)
    return account


def health_check_account(session: Session, account_id: int) -> TgAccount:
    account = _ensure_account_available(session.get(TgAccount, account_id))
    try:
        credentials = credentials_for_account(session, account)
    except ValueError as exc:
        account.status = AccountStatus.NEED_RELOGIN.value
        account.health_score = min(account.health_score, 45)
        audit(session, tenant_id=account.tenant_id, actor="system", action="账号健康检查", target_type="tg_account", target_id=str(account.id), detail=str(exc))
        session.commit()
        session.refresh(account)
        return account
    result = gateway.check_account_health(account.session_ciphertext, credentials)
    account.status = result.status
    account.health_score = result.health_score
    account.last_active_at = _now() if result.status == AccountStatus.ACTIVE.value else account.last_active_at
    audit(session, tenant_id=account.tenant_id, actor="system", action="账号健康检查", target_type="tg_account", target_id=str(account.id), detail=result.detail)
    session.commit()
    session.refresh(account)
    return account


def sync_remote_profile(session: Session, account_id: int, actor: str) -> TgAccount:
    account = _ensure_account_available(session.get(TgAccount, account_id))
    credentials = credentials_for_account(session, account)
    profile = gateway.pull_profile(account.id, account.session_ciphertext, credentials)
    account.tg_first_name = profile.first_name
    account.tg_last_name = profile.last_name
    account.tg_bio = profile.bio
    if profile.username:
        account.username = profile.username
    account.profile_synced_at = _now()
    account.profile_sync_status = "已同步"
    account.profile_sync_error = ""
    audit(session, tenant_id=account.tenant_id, actor=actor, action="拉取TG账号资料", target_type="tg_account", target_id=str(account.id))
    session.commit()
    session.refresh(account)
    return account


def sync_groups(session: Session, account_id: int, actor: str = "普通用户") -> list[TgGroup]:
    account = _ensure_account_available(session.get(TgAccount, account_id))

    credentials = credentials_for_account(session, account)
    snapshots = gateway.list_groups(account.id, account.session_ciphertext, credentials)
    groups: list[TgGroup] = []
    for snapshot in snapshots:
        group = session.scalar(
            select(TgGroup).where(TgGroup.tenant_id == account.tenant_id, TgGroup.tg_peer_id == snapshot.tg_peer_id)
        )
        if not group:
            group = TgGroup(
                tenant_id=account.tenant_id,
                tg_peer_id=snapshot.tg_peer_id,
                title=snapshot.title,
                group_type=snapshot.group_type,
                member_count=snapshot.member_count,
                can_send=snapshot.can_send,
                auth_status=GroupAuthStatus.AUTHORIZED.value if snapshot.can_send else GroupAuthStatus.UNVERIFIED.value,
            )
            session.add(group)
            session.flush()
        else:
            group.title = snapshot.title
            group.member_count = snapshot.member_count
            group.can_send = snapshot.can_send
            if snapshot.can_send and group.auth_status == GroupAuthStatus.UNVERIFIED.value:
                group.auth_status = GroupAuthStatus.AUTHORIZED.value
        groups.append(group)
        exists = session.scalar(
            select(TgGroupAccount).where(TgGroupAccount.group_id == group.id, TgGroupAccount.account_id == account.id)
        )
        if not exists:
            session.add(
                TgGroupAccount(
                    tenant_id=account.tenant_id,
                    group_id=group.id,
                    account_id=account.id,
                    permission_label=snapshot.permission_label,
                    can_send=bool(snapshot.can_send),
                )
            )
        else:
            exists.permission_label = snapshot.permission_label
            exists.can_send = bool(snapshot.can_send)
        session.flush()
        group_links = list(
            session.scalars(
                select(TgGroupAccount).where(
                    TgGroupAccount.tenant_id == account.tenant_id,
                    TgGroupAccount.group_id == group.id,
                )
            )
        )
        group.can_send = any(link.can_send for link in group_links)
        if group.can_send:
            group.auth_status = GroupAuthStatus.AUTHORIZED.value
        elif group.auth_status == GroupAuthStatus.AUTHORIZED.value:
            group.auth_status = GroupAuthStatus.READONLY.value
        if not snapshot.can_send:
            create_verification_task(
                session,
                tenant_id=account.tenant_id,
                account_id=account.id,
                group_id=group.id,
                message_task_id=None,
                verification_type="群发言验证",
                detected_reason="该账号在群内暂不可发言，可能需要关注频道、按钮验证或人工确认。",
                suggested_action="人工处理",
                target_peer_id=group.tg_peer_id,
                target_display=group.title,
            )

    audit(session, tenant_id=account.tenant_id, actor=actor, action="同步账号群聊", target_type="tg_account", target_id=str(account.id), detail=f"synced={len(groups)}")
    session.commit()
    return groups


def account_groups(session: Session, account_id: int) -> list[dict]:
    account = session.get(TgAccount, account_id)
    if not account:
        raise ValueError("account not found")
    rows = session.execute(
        select(TgGroup, TgGroupAccount)
        .join(TgGroupAccount, TgGroupAccount.group_id == TgGroup.id)
        .where(TgGroup.tenant_id == account.tenant_id, TgGroupAccount.account_id == account.id)
        .order_by(TgGroup.id.desc())
    ).all()
    result = []
    for group, link in rows:
        item = {
            **{key: getattr(group, key) for key in [
                "id",
                "tenant_id",
                "tg_peer_id",
                "title",
                "group_type",
                "member_count",
                "auth_status",
                "can_send",
                "active_window",
                "daily_limit",
                "account_cooldown_seconds",
                "group_cooldown_seconds",
                "topic_direction",
                "banned_words",
                "link_whitelist",
                "require_review",
            ]},
            "permission_label": link.permission_label,
            "account_can_send": link.can_send,
            "last_sent_at": link.last_sent_at,
        }
        result.append(item)
    return result


def account_message_records(session: Session, account_id: int, limit: int = 100) -> list[MessageTask]:
    account = session.get(TgAccount, account_id)
    if not account:
        raise ValueError("account not found")
    task_ids_from_attempts = select(MessageTaskAttempt.task_id).where(MessageTaskAttempt.account_id == account.id)
    return list(
        session.scalars(
            select(MessageTask)
            .where(
                MessageTask.tenant_id == account.tenant_id,
                or_(MessageTask.account_id == account.id, MessageTask.id.in_(task_ids_from_attempts)),
            )
            .order_by(MessageTask.id.desc())
            .limit(limit)
        )
    )


def account_contacts(session: Session, account_id: int, limit: int = 200) -> list[TgContact]:
    account = session.get(TgAccount, account_id)
    if not account:
        raise ValueError("account not found")
    return list(
        session.scalars(
            select(TgContact)
            .where(TgContact.tenant_id == account.tenant_id, TgContact.account_id == account.id)
            .order_by(TgContact.last_synced_at.desc(), TgContact.id.desc())
            .limit(limit)
        )
    )


def find_account_contact(session: Session, account: TgAccount, target_peer_id: str) -> TgContact | None:
    normalized = target_peer_id.strip()
    candidates = [normalized]
    if normalized.startswith("@"):
        candidates.append(normalized[1:])
    else:
        candidates.append(f"@{normalized}")
    contacts = list(
        session.scalars(
            select(TgContact).where(
                TgContact.tenant_id == account.tenant_id,
                TgContact.account_id == account.id,
                or_(TgContact.peer_id.in_(candidates), TgContact.username.in_([item.lstrip("@") for item in candidates])),
            )
        )
    )
    return contacts[0] if contacts else None


def _contact_phone_mask(phone: str | None) -> str:
    return mask_phone(phone) if phone else ""


def _risk_item(
    *,
    level: str,
    code: str,
    title: str,
    detail: str,
    source: str,
    action: str,
    occurred_at: datetime | None = None,
) -> dict:
    return {
        "level": level,
        "code": code,
        "title": title,
        "detail": detail,
        "source": source,
        "action": action,
        "occurred_at": occurred_at,
    }


def account_risk_diagnostics(
    account: TgAccount,
    *,
    sync_records: list[TgAccountSyncRecord],
    profile_records: list[TgAccountProfileSyncRecord],
    message_records: list[MessageTask],
    manual_records: list,
    operation_attempts: list,
    verification_tasks: list,
    groups: list[dict],
    operation_targets: list,
) -> list[dict]:
    risks: list[dict] = []
    failure_actions = {
        FailureType.FLOOD_WAIT.value: ("高", "触发 FloodWait", "账号最近触发 Telegram FloodWait。", "暂停该账号任务，等待失败详情中的时间后再重试。"),
        FailureType.ACCOUNT_LIMITED.value: ("高", "账号受限", "账号最近被判定为受限或不可用。", "系统每小时自动探测恢复；必要时执行健康检查，确认需要时再重新登录。"),
        FailureType.ACCOUNT_UNAVAILABLE.value: ("中", "账号不可用", "账号最近执行时不可用。", "重新登录或检查 session。"),
        FailureType.PEER_INVALID.value: ("中", "目标不可访问", "账号最近访问目标失败。", "同步群/频道目标，确认账号仍在目标内。"),
        FailureType.GROUP_PERMISSION_DENIED.value: ("中", "群无发言权限", "账号最近在群内没有发言权限。", "进入账号详情执行解除群限制，管理员处理后重查目标能力。"),
        FailureType.CHANNEL_POST_DENIED.value: ("中", "频道无发帖权限", "账号最近没有频道发帖权限。", "使用有频道发帖权限的账号。"),
        FailureType.COMMENT_UNAVAILABLE.value: ("低", "评论区不可用", "频道消息不支持回复或账号无法进入评论区。", "改用查看/点赞任务，或确认频道讨论区设置。"),
        FailureType.REACTION_UNAVAILABLE.value: ("低", "Reaction 不可用", "频道消息不支持该 Reaction。", "更换 Reaction 或跳过点赞任务。"),
    }
    failure_events: list[tuple[datetime | None, str, str, str]] = []
    for attempt in operation_attempts:
        if getattr(attempt, "failure_type", ""):
            failure_events.append((attempt.executed_at, attempt.failure_type, attempt.failure_detail, f"运营任务 #{attempt.task_id}"))
    for record in manual_records:
        if getattr(record, "failure_type", ""):
            failure_events.append((record.created_at, record.failure_type, record.failure_detail, "立即发送"))
    for task in message_records:
        if getattr(task, "failure_type", ""):
            failure_events.append((getattr(task, "sent_at", None) or getattr(task, "scheduled_at", None), task.failure_type, getattr(task, "failure_detail", ""), f"消息任务 #{task.id}"))
    for record in sync_records:
        if record.status == "失败":
            failure_events.append((record.finished_at or record.created_at, record.failure_type or "同步失败", record.failure_detail, f"同步 {record.sync_type}"))
    for record in profile_records:
        if record.status == "失败":
            failure_events.append((record.synced_at or record.created_at, record.failure_type or "资料同步失败", record.failure_detail, "资料同步"))
    sorted_failure_events = sorted(failure_events, key=lambda item: item[0] or datetime.min, reverse=True)

    def _recent_failure_detail(*failure_types: str) -> tuple[str, datetime | None, str] | None:
        for wanted_type in failure_types:
            for occurred_at, failure_type, detail, source in sorted_failure_events:
                if failure_type != wanted_type:
                    continue
                _, title, fallback_detail, _ = failure_actions.get(failure_type, ("低", failure_type or "未知失败", "账号最近存在失败记录。", "查看执行记录定位原因。"))
                return detail or fallback_detail, occurred_at, f"{source}：{title}"
        return None

    status_level = {
        AccountStatus.BANNED.value: ("高", "账号已封禁", "账号状态已标记为封禁，暂停所有发送动作。", "更换账号或重新登录确认。"),
        AccountStatus.SUSPECTED_BANNED.value: ("高", "疑似封禁", "账号状态已标记为疑似封禁，需要人工确认。", "立即做健康检查，并在 TG 客户端确认账号状态。"),
        AccountStatus.LIMITED.value: ("高", "账号受限", "账号最近触发受限类错误，系统已暂停或降低派单优先级。", "系统每小时自动探测恢复；需要重新登录时再执行重新登录。"),
        AccountStatus.SESSION_EXPIRED.value: ("高", "Session 失效", "账号 session 不再可用。", "重新登录账号。"),
        AccountStatus.NEED_RELOGIN.value: ("中", "需重新登录", "账号缺少可用 session 或开发者应用凭证不可用。", "重新登录账号并检查开发者应用。"),
        AccountStatus.ERROR.value: ("中", "账号异常", "账号处于异常状态。", "执行健康检查并查看最近失败记录。"),
        AccountStatus.DISABLED.value: ("中", "账号已禁用", "账号已从可执行池移除。", "需要运营时先恢复或重新添加账号。"),
    }
    if account.status in status_level:
        level, title, detail, action = status_level[account.status]
        source = "账号状态"
        occurred_at = account.last_active_at
        if account.status == AccountStatus.LIMITED.value:
            recent = _recent_failure_detail(
                FailureType.ACCOUNT_LIMITED.value,
                FailureType.FLOOD_WAIT.value,
                FailureType.GROUP_PERMISSION_DENIED.value,
                FailureType.SLOWMODE.value,
            )
            if recent:
                recent_detail, recent_at, recent_source = recent
                detail = f"{recent_detail}。系统已暂停或降低该账号派单；账号级限制需要等待 TG 限制解除并由系统探测恢复，群内拦截需管理员解除后重查。"
                source = recent_source
                occurred_at = recent_at or occurred_at
        risks.append(_risk_item(level=level, code="ACCOUNT_STATUS", title=title, detail=detail, source=source, action=action, occurred_at=occurred_at))
    if not account.session_ciphertext and account.status not in {AccountStatus.PENDING_LOGIN.value, AccountStatus.WAITING_CODE.value, AccountStatus.WAITING_QR.value, AccountStatus.WAITING_2FA.value}:
        risks.append(_risk_item(level="中", code="SESSION_MISSING", title="缺少 Session", detail="账号没有可用 session，无法执行真实 TG 操作。", source="账号状态", action="重新登录账号。"))
    if account.health_score < 60:
        risks.append(_risk_item(level="中", code="LOW_HEALTH", title="健康分偏低", detail=f"当前健康分 {round(account.health_score)}，建议减少任务量。", source="健康分", action="查看失败记录并执行健康检查。", occurred_at=account.last_active_at))
    if account.developer_app and account.developer_app.health_status != "健康":
        risks.append(_risk_item(level="中", code="DEVELOPER_APP_UNHEALTHY", title="开发者应用异常", detail=f"底层开发者应用状态为 {account.developer_app.health_status}。", source="开发者应用", action="检查开发者应用 api_id/api_hash。", occurred_at=account.developer_app.last_check_at))
    pending_tasks = [task for task in verification_tasks if getattr(task, "status", "") in {"待处理", "失败", "需人工处理"}]
    if pending_tasks:
        risks.append(_risk_item(level="中", code="PENDING_VERIFICATION", title="存在待处理验证", detail=f"有 {len(pending_tasks)} 个账号验证或群限制事项待处理；账号级受限由系统探测恢复，群内拦截需要管理员解除后重查。", source="验证任务", action="进入验证待处理页签，按账号级受限或解除群限制分流处理。", occurred_at=getattr(pending_tasks[0], "created_at", None)))
    seen_failure_types: set[str] = set()
    for occurred_at, failure_type, detail, source in sorted_failure_events:
        if failure_type in seen_failure_types:
            continue
        level, title, fallback_detail, action = failure_actions.get(failure_type, ("低", failure_type or "未知失败", "账号最近存在失败记录。", "查看执行记录定位原因。"))
        risks.append(_risk_item(level=level, code="RECENT_FAILURE", title=title, detail=detail or fallback_detail, source=source, action=action, occurred_at=occurred_at))
        seen_failure_types.add(failure_type)
        if len(seen_failure_types) >= 5:
            break

    readonly_groups = [group for group in groups if not group.get("account_can_send")]
    readonly_targets = [target for target in operation_targets if not getattr(target, "can_send", True)]
    if readonly_groups or readonly_targets:
        risks.append(_risk_item(level="低", code="TARGET_READONLY", title="部分目标只读", detail=f"{len(readonly_groups) + len(readonly_targets)} 个群/频道目标当前不可发送。", source="群/频道目标", action="群内拦截先由管理员解除，再同步或重查目标权限。"))

    level_order = {"高": 0, "中": 1, "低": 2}
    return sorted(risks, key=lambda item: (level_order.get(item["level"], 9), 0 if item["code"] == "ACCOUNT_STATUS" else 1, item["occurred_at"] or datetime.min))[:12]


def sync_account_contacts(session: Session, account_id: int, actor: str) -> list[TgContact]:
    account = _ensure_account_available(session.get(TgAccount, account_id))
    try:
        credentials = credentials_for_account(session, account)
        snapshots = gateway.list_contacts(account.id, account.session_ciphertext, credentials)
    except Exception as exc:  # noqa: BLE001 - preserve real gateway detail for operator.
        audit(session, tenant_id=account.tenant_id, actor=actor, action="同步联系人失败", target_type="tg_account", target_id=str(account.id), detail=str(exc))
        session.commit()
        if get_settings().tg_gateway_mode != "mock":
            raise ValueError(f"同步联系人失败：{exc}") from exc
        snapshots = [
            ContactSnapshot(f"mock-user-{account.id}-1", "测试联系人 Alice", "alice_ops", "+8613812345678", "private", True),
            ContactSnapshot(f"mock-user-{account.id}-2", "私聊对象 Bob", "bob_growth", "", "private", False),
            ContactSnapshot(f"mock-member-{account.id}-1", "星火群友 Leo", "spark_leo", "", "group_member", False),
            ContactSnapshot(f"mock-member-{account.id}-2", "内测群友 Mia", "mimo_mia", "", "group_member", False),
        ]
    now_value = _now()
    for snapshot in snapshots:
        if not snapshot.peer_id:
            continue
        contact = session.scalar(
            select(TgContact).where(TgContact.account_id == account.id, TgContact.peer_id == snapshot.peer_id)
        )
        if not contact:
            contact = TgContact(
                tenant_id=account.tenant_id,
                account_id=account.id,
                peer_id=snapshot.peer_id,
                created_at=now_value,
            )
            session.add(contact)
        contact.display_name = snapshot.display_name or snapshot.username or snapshot.peer_id
        contact.username = snapshot.username
        contact.phone_masked = _contact_phone_mask(snapshot.phone)
        contact.phone_ciphertext = encrypt_secret(snapshot.phone.strip()) if snapshot.phone and snapshot.phone.strip() else None
        contact.contact_type = snapshot.contact_type or "private"
        contact.is_mutual = bool(snapshot.is_mutual)
        contact.last_message_at = snapshot.last_message_at.replace(tzinfo=None) if snapshot.last_message_at and snapshot.last_message_at.tzinfo else snapshot.last_message_at
        contact.last_synced_at = now_value
    audit(session, tenant_id=account.tenant_id, actor=actor, action="同步联系人", target_type="tg_account", target_id=str(account.id), detail=f"contacts={len(snapshots)}")
    session.commit()
    return account_contacts(session, account_id)


def list_verification_codes(session: Session, account_id: int, actor: str = "普通用户", reason: str = "") -> list[TgVerificationCode]:
    account = session.get(TgAccount, account_id)
    if not account:
        raise ValueError("account not found")
    codes = list(
        session.scalars(
            select(TgVerificationCode)
            .where(TgVerificationCode.tenant_id == account.tenant_id, TgVerificationCode.account_id == account.id)
            .order_by(TgVerificationCode.id.desc())
            .limit(20)
        )
    )
    for code in codes:
        if code.code_preview and _is_expired(code.expires_at):
            code.code_preview = None
            code.status = "已过期"
        elif code.code_preview and not code.viewed_at:
            code.viewed_at = _now()
            code.viewed_by = actor
    audit(session, tenant_id=account.tenant_id, actor=actor, action="查看TG验证码", target_type="tg_account", target_id=str(account.id), detail=f"reason={reason}; codes={len(codes)}")
    session.commit()
    return codes


def poll_account_verification_codes(session: Session, account_id: int, actor: str, reason: str) -> list[TgVerificationCode]:
    account = _ensure_account_available(session.get(TgAccount, account_id))
    try:
        credentials = credentials_for_account(session, account)
        snapshots = gateway.poll_verification_codes(account.id, account.session_ciphertext, credentials)
    except Exception as exc:  # noqa: BLE001 - keep mock-mode/local debugging friendly.
        audit(session, tenant_id=account.tenant_id, actor=actor, action="同步TG官方验证码失败", target_type="tg_account", target_id=str(account.id), detail=f"reason={reason}; error={exc}")
        session.commit()
        if get_settings().tg_gateway_mode != "mock":
            raise ValueError(f"同步TG官方验证码失败：{exc}") from exc
        snapshots = []
    if not snapshots and get_settings().tg_gateway_mode == "mock":
        snapshots = [
            VerificationCodeSnapshot(
                code=f"{random.randint(10000, 99999)}",
                expires_at=_now() + timedelta(minutes=3),
                raw_hint="TG 官方服务消息验证码（mock）",
            )
        ]
    for snapshot in snapshots:
        duplicate = session.scalar(
            select(TgVerificationCode)
            .where(
                TgVerificationCode.tenant_id == account.tenant_id,
                TgVerificationCode.account_id == account.id,
                TgVerificationCode.source == "telegram_service_message",
                TgVerificationCode.code_preview == snapshot.code,
                TgVerificationCode.expires_at > _now(),
            )
            .order_by(TgVerificationCode.id.desc())
        )
        if duplicate:
            continue
        session.add(
            TgVerificationCode(
                tenant_id=account.tenant_id,
                account_id=account.id,
                source="telegram_service_message",
                code_preview=snapshot.code,
                expires_at=snapshot.expires_at,
                raw_hint=snapshot.raw_hint,
            )
        )
    audit(session, tenant_id=account.tenant_id, actor=actor, action="同步TG官方验证码", target_type="tg_account", target_id=str(account.id), detail=f"reason={reason}; codes={len(snapshots)}")
    session.commit()
    return list_verification_codes(session, account_id, actor, reason)


def account_detail(session: Session, account_id: int, actor: str, *, include_verification_codes: bool = True) -> dict:
    account = session.get(TgAccount, account_id)
    if not account:
        raise ValueError("account not found")
    stale_profile_count = _mark_stale_profile_sync_records(session, account.id)
    stale_sync_count = _mark_stale_account_sync_records(session, account.id)
    if stale_profile_count or stale_sync_count:
        session.commit()
        session.refresh(account)
    login_flows = list_login_flows(session, account_id)[:10]
    codes = list_verification_codes(session, account_id, actor) if include_verification_codes else []
    profile_records = list_profile_sync_records(session, account_id)
    sync_records = list_account_sync_records(session, account_id)
    contacts = account_contacts(session, account_id)
    groups = account_groups(session, account_id)
    records = account_message_records(session, account_id, 50)
    from .operations import filter_operation_targets, list_manual_operations, list_operation_attempts

    operation_targets = filter_operation_targets(session, account.tenant_id)
    manual_records = list_manual_operations(session, account.tenant_id, account.id)
    operation_attempts = [attempt for attempt in list_operation_attempts(session, account.tenant_id) if attempt.account_id == account.id][:50]
    from .cloning import account_clone_plans

    clone_plans = account_clone_plans(session, account.tenant_id, account_id=account.id, limit=8)
    verification_tasks = list_verification_tasks(session, account.tenant_id, account_id=account.id, limit=8)
    risk_diagnostics = account_risk_diagnostics(
        account,
        sync_records=sync_records,
        profile_records=profile_records,
        message_records=records,
        manual_records=manual_records,
        operation_attempts=operation_attempts,
        verification_tasks=verification_tasks,
        groups=groups,
        operation_targets=operation_targets,
    )
    pending_verification_count = sum(1 for task in verification_tasks if task.status in {"待处理", "失败", "需人工处理"})
    sent_count = sum(1 for task in records if task.status == TaskStatus.SENT.value)
    failed_count = sum(1 for task in records if task.status == TaskStatus.FAILED.value)
    now_value = _now()
    latest_sync_time = next((record.finished_at for record in sync_records if record.status == "已同步" and record.finished_at), None)
    if not latest_sync_time and sync_records:
        latest_sync_time = sync_records[0].created_at
    pending_due = any(record.status == "排队中" and record.scheduled_at <= now_value for record in sync_records)
    pending_future_at = min((record.scheduled_at for record in sync_records if record.status == "排队中" and record.scheduled_at > now_value), default=None)
    running_sync = any(record.status == "同步中" for record in sync_records)
    next_sync_at = None
    sync_due = False
    auto_sync_enabled = account.status not in ACCOUNT_AUTO_SYNC_SKIP_STATUSES and bool(account.session_ciphertext)
    sync_status_text = "账号不在线，暂停自动同步"
    if auto_sync_enabled:
        if running_sync:
            sync_status_text = "同步中"
        elif pending_due:
            sync_due = True
            sync_status_text = "已到同步时间，等待后台执行"
        elif pending_future_at:
            next_sync_at = pending_future_at
            sync_status_text = "已错峰排队，等待后台执行"
        elif latest_sync_time:
            planned_sync_at = latest_sync_time + ACCOUNT_SYNC_INTERVAL
            if planned_sync_at <= now_value:
                sync_due = True
                sync_status_text = "已到同步时间，等待后台执行"
            else:
                next_sync_at = planned_sync_at
                sync_status_text = "自动同步已排程"
        else:
            sync_due = True
            sync_status_text = "尚未同步，等待后台执行"
        if account.status == AccountStatus.LIMITED.value and sync_status_text == "自动同步已排程":
            sync_status_text = "账号受限，系统每小时自动探测恢复"
    return {
        "account": account,
        "risk_diagnostics": risk_diagnostics,
        "login_flows": login_flows,
        "verification_codes": codes,
        "profile_sync_records": profile_records,
        "sync_records": sync_records,
        "next_sync_at": next_sync_at,
        "sync_due": sync_due,
        "sync_status_text": sync_status_text,
        "contacts": contacts,
        "groups": groups,
        "operation_targets": operation_targets,
        "message_records": records,
        "manual_operation_records": manual_records,
        "operation_task_attempts": operation_attempts,
        "clone_plans": clone_plans,
        "verification_tasks": verification_tasks,
        "stats": {
            "joined_groups": len(groups),
            "contacts": len(contacts),
            "message_records": len(records),
            "operation_targets": len(operation_targets),
            "manual_operation_records": len(manual_records),
            "operation_task_attempts": len(operation_attempts),
            "sent": sent_count,
            "failed": failed_count,
            "clone_plans": len(clone_plans),
            "verification_tasks": len(verification_tasks),
            "pending_verification_tasks": pending_verification_count,
            "risk_diagnostics": len(risk_diagnostics),
            "high_risk_diagnostics": sum(1 for item in risk_diagnostics if item["level"] == "高"),
        },
    }


def filter_accounts(session: Session, tenant_id: int, page: int, page_size: int, search: str | None, status: str | None, pool_id: int | None = None, include_deleted: bool = False) -> list[TgAccount]:
    require_tenant(session, tenant_id)
    seed_account_pools(session)
    stmt = select(TgAccount).where(TgAccount.tenant_id == tenant_id)
    if not include_deleted:
        stmt = stmt.where(TgAccount.deleted_at.is_(None))
    if status:
        stmt = stmt.where(TgAccount.status == status)
    if pool_id:
        pool = session.get(AccountPool, pool_id)
        if not pool or pool.tenant_id != tenant_id:
            raise ValueError("account pool not found")
        stmt = stmt.where(TgAccount.pool_id == pool.id)
    needle = (search or "").strip().lower()
    if needle:
        accounts = list(session.scalars(stmt.order_by(TgAccount.id)))

        def matches(account: TgAccount) -> bool:
            values = [
                account.display_name,
                account.username,
                account.phone_masked,
                get_account_phone(account),
            ]
            return any(needle in str(value).lower() for value in values if value)

        offset = (page - 1) * page_size
        return [account for account in accounts if matches(account)][offset : offset + page_size]
    return list(session.scalars(stmt.order_by(TgAccount.id).offset((page - 1) * page_size).limit(page_size)))
