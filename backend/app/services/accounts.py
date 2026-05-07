from __future__ import annotations

import json
import random
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.gateways import ContactSnapshot, VerificationCodeSnapshot
from app.models import (
    AccountPool,
    AccountStatus,
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
from .developer_apps import credentials_for_account
from .tenants import ensure_account_quota_available
from .verification import list_verification_tasks, create_verification_task
from .account_pools import account_pool_snapshot, ensure_default_account_pool, seed_account_pools

__all__ = [
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
    "start_login",
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
    account = TgAccount(**data)
    session.add(account)
    session.flush()
    audit(session, tenant_id=account.tenant_id, actor=actor, action="添加TG账号", target_type="tg_account", target_id=str(account.id))
    session.commit()
    session.refresh(account)
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


def upload_account_avatar(session: Session, account_id: int, filename: str, content_type: str, data: bytes, actor: str) -> dict:
    account = session.get(TgAccount, account_id)
    if not account:
        raise ValueError("account not found")
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
    account = session.get(TgAccount, account_id)
    if not account:
        raise ValueError("account not found")
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
    account = session.get(TgAccount, account_id)
    if not account:
        raise ValueError("account not found")
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
    if not account or account.tenant_id != record.tenant_id:
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
    sync_types = sync_types or ["groups", "contacts", "codes", "health", "profile_pull"]
    records: list[TgAccountSyncRecord] = []
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
            records.append(existing)
            continue
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


def queue_account_sync_now(session: Session, account_id: int, actor: str, sync_types: list[str] | None = None) -> list[TgAccountSyncRecord]:
    account = session.get(TgAccount, account_id)
    if not account:
        raise ValueError("account not found")
    records = queue_account_sync_records(session, account, trigger_source="manual", sync_types=sync_types)
    audit(session, tenant_id=account.tenant_id, actor=actor, action="手动同步账号数据", target_type="tg_account", target_id=str(account.id))
    session.commit()
    return list_account_sync_records(session, account.id, limit=len(records) + 5)


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
        if not account:
            raise ValueError("account not found")
        if record.sync_type == "groups":
            result_count = len(sync_groups(session, account.id, actor="tg-worker"))
        elif record.sync_type == "contacts":
            result_count = len(sync_account_contacts(session, account.id, "tg-worker"))
        elif record.sync_type == "codes":
            result_count = len(poll_account_verification_codes(session, account.id, "tg-worker"))
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


def drain_account_sync_records(session_factory, limit: int = 20) -> int:
    count = 0
    with session_factory() as session:
        cutoff = _now() - timedelta(hours=6)
        active_accounts = list(
            session.scalars(
                select(TgAccount)
                .where(TgAccount.status == AccountStatus.ACTIVE.value)
                .order_by(TgAccount.id.asc())
                .limit(50)
            )
        )
        for account in active_accounts:
            latest = session.scalar(
                select(TgAccountSyncRecord)
                .where(TgAccountSyncRecord.account_id == account.id)
                .order_by(TgAccountSyncRecord.created_at.desc())
                .limit(1)
            )
            if not latest or latest.created_at <= cutoff:
                queue_account_sync_records(session, account, trigger_source="scheduled", sync_types=["health", "profile_pull", "groups", "contacts", "codes"])
        session.commit()
        record_ids = list(
            session.scalars(
                select(TgAccountSyncRecord.id)
                .where(TgAccountSyncRecord.status == "排队中", TgAccountSyncRecord.scheduled_at <= _now())
                .order_by(TgAccountSyncRecord.scheduled_at.asc(), TgAccountSyncRecord.id.asc())
                .limit(limit)
            )
        )
    for record_id in record_ids:
        with session_factory() as session:
            process_account_sync_record(session, record_id)
            count += 1
    return count


def start_login(session: Session, account_id: int, method: str, actor: str = "普通用户") -> TgLoginFlow:
    account = session.get(TgAccount, account_id)
    if not account:
        raise ValueError("account not found")

    credentials = credentials_for_account(session, account, assign_if_missing=True)
    phone = get_account_phone(account)
    challenge = gateway.start_login(method, account_id=account.id, phone=phone, credentials=credentials)
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


def list_login_flows(session: Session, account_id: int) -> list[TgLoginFlow]:
    account = session.get(TgAccount, account_id)
    if not account:
        raise ValueError("account not found")
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
    account = session.get(TgAccount, account_id)
    if not account:
        raise ValueError("account not found")

    latest_flow = session.scalar(
        select(TgLoginFlow)
        .where(TgLoginFlow.account_id == account_id)
        .order_by(TgLoginFlow.id.desc())
        .limit(1)
    )
    if latest_flow and latest_flow.code_preview and _is_expired(latest_flow.code_expires_at) and not password_2fa:
        latest_flow.code_preview = None
        latest_flow.status = "已过期"
        account.status = AccountStatus.ERROR.value
        audit(session, tenant_id=account.tenant_id, actor=actor, action="验证TG登录失败", target_type="tg_account", target_id=str(account.id), detail="code expired")
        session.commit()
        session.refresh(account)
        return account

    credentials = credentials_for_account(session, account)
    status, raw_session = gateway.finish_login(code, password_2fa, account_id=account.id, phone=get_account_phone(account), credentials=credentials)
    account.status = status
    if raw_session:
        account.session_ciphertext = encrypt_session(raw_session)
        account.last_active_at = _now()
        account.health_score = max(account.health_score, 90)
        if latest_flow:
            latest_flow.code_preview = None
            latest_flow.status = status
        if status == AccountStatus.ACTIVE.value:
            queue_account_sync_records(session, account, trigger_source="login")

    audit(session, tenant_id=account.tenant_id, actor=actor, action="验证TG登录", target_type="tg_account", target_id=str(account.id), detail=f"status={status}")
    session.commit()
    session.refresh(account)
    return account


def check_qr_login(session: Session, account_id: int, actor: str = "普通用户") -> TgAccount:
    account = session.get(TgAccount, account_id)
    if not account:
        raise ValueError("account not found")
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
    if raw_session:
        account.session_ciphertext = encrypt_session(raw_session)
        account.last_active_at = _now()
        account.health_score = max(account.health_score, 90)
        if status == AccountStatus.ACTIVE.value:
            queue_account_sync_records(session, account, trigger_source="login")
    latest_flow.status = status
    audit(session, tenant_id=account.tenant_id, actor=actor, action="检查QR登录", target_type="tg_account", target_id=str(account.id), detail=f"status={status}")
    session.commit()
    session.refresh(account)
    return account


def health_check_account(session: Session, account_id: int) -> TgAccount:
    account = session.get(TgAccount, account_id)
    if not account:
        raise ValueError("account not found")
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
    account = session.get(TgAccount, account_id)
    if not account:
        raise ValueError("account not found")
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
    account = session.get(TgAccount, account_id)
    if not account:
        raise ValueError("account not found")

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
            )
            session.add(group)
            session.flush()
        else:
            group.title = snapshot.title
            group.member_count = snapshot.member_count
            group.can_send = snapshot.can_send
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
                    can_send=snapshot.can_send and group.auth_status == GroupAuthStatus.AUTHORIZED.value,
                )
            )
        else:
            exists.permission_label = snapshot.permission_label
            exists.can_send = snapshot.can_send and group.auth_status == GroupAuthStatus.AUTHORIZED.value
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


def sync_account_contacts(session: Session, account_id: int, actor: str) -> list[TgContact]:
    account = session.get(TgAccount, account_id)
    if not account:
        raise ValueError("account not found")
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
        contact.contact_type = snapshot.contact_type or "private"
        contact.is_mutual = bool(snapshot.is_mutual)
        contact.last_message_at = snapshot.last_message_at.replace(tzinfo=None) if snapshot.last_message_at and snapshot.last_message_at.tzinfo else snapshot.last_message_at
        contact.last_synced_at = now_value
    audit(session, tenant_id=account.tenant_id, actor=actor, action="同步联系人", target_type="tg_account", target_id=str(account.id), detail=f"contacts={len(snapshots)}")
    session.commit()
    return account_contacts(session, account_id)


def list_verification_codes(session: Session, account_id: int, actor: str = "普通用户") -> list[TgVerificationCode]:
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
    changed = False
    for code in codes:
        if code.code_preview and _is_expired(code.expires_at):
            code.code_preview = None
            code.status = "已过期"
            changed = True
        elif code.code_preview and not code.viewed_at:
            code.viewed_at = _now()
            code.viewed_by = actor
            changed = True
    if changed:
        audit(session, tenant_id=account.tenant_id, actor=actor, action="查看TG验证码", target_type="tg_account", target_id=str(account.id))
        session.commit()
    return codes


def poll_account_verification_codes(session: Session, account_id: int, actor: str) -> list[TgVerificationCode]:
    account = session.get(TgAccount, account_id)
    if not account:
        raise ValueError("account not found")
    try:
        credentials = credentials_for_account(session, account)
        snapshots = gateway.poll_verification_codes(account.id, account.session_ciphertext, credentials)
    except Exception as exc:  # noqa: BLE001 - keep mock-mode/local debugging friendly.
        audit(session, tenant_id=account.tenant_id, actor=actor, action="同步TG官方验证码失败", target_type="tg_account", target_id=str(account.id), detail=str(exc))
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
    audit(session, tenant_id=account.tenant_id, actor=actor, action="同步TG官方验证码", target_type="tg_account", target_id=str(account.id), detail=f"codes={len(snapshots)}")
    session.commit()
    return list_verification_codes(session, account_id, actor)


def account_detail(session: Session, account_id: int, actor: str) -> dict:
    account = session.get(TgAccount, account_id)
    if not account:
        raise ValueError("account not found")
    login_flows = list_login_flows(session, account_id)[:10]
    codes = list_verification_codes(session, account_id, actor)
    profile_records = list_profile_sync_records(session, account_id)
    sync_records = list_account_sync_records(session, account_id)
    contacts = account_contacts(session, account_id)
    groups = account_groups(session, account_id)
    records = account_message_records(session, account_id, 50)
    from .cloning import account_clone_plans

    clone_plans = account_clone_plans(session, account.tenant_id, account_id=account.id, limit=8)
    verification_tasks = list_verification_tasks(session, account.tenant_id, account_id=account.id, limit=8)
    sent_count = sum(1 for task in records if task.status == TaskStatus.SENT.value)
    failed_count = sum(1 for task in records if task.status == TaskStatus.FAILED.value)
    latest_sync_time = (sync_records[0].finished_at or sync_records[0].created_at) if sync_records else None
    next_sync_at = latest_sync_time + timedelta(hours=6) if latest_sync_time and account.status == AccountStatus.ACTIVE.value else None
    return {
        "account": account,
        "login_flows": login_flows,
        "verification_codes": codes,
        "profile_sync_records": profile_records,
        "sync_records": sync_records,
        "next_sync_at": next_sync_at,
        "contacts": contacts,
        "groups": groups,
        "message_records": records,
        "clone_plans": clone_plans,
        "verification_tasks": verification_tasks,
        "stats": {
            "joined_groups": len(groups),
            "contacts": len(contacts),
            "message_records": len(records),
            "sent": sent_count,
            "failed": failed_count,
            "clone_plans": len(clone_plans),
            "verification_tasks": len(verification_tasks),
        },
    }


def filter_accounts(session: Session, tenant_id: int, page: int, page_size: int, search: str | None, status: str | None, pool_id: int | None = None) -> list[TgAccount]:
    require_tenant(session, tenant_id)
    seed_account_pools(session)
    stmt = select(TgAccount).where(TgAccount.tenant_id == tenant_id)
    if search:
        like = f"%{search}%"
        stmt = stmt.where(or_(TgAccount.display_name.like(like), TgAccount.username.like(like), TgAccount.phone_masked.like(like)))
    if status:
        stmt = stmt.where(TgAccount.status == status)
    if pool_id:
        pool = session.get(AccountPool, pool_id)
        if not pool or pool.tenant_id != tenant_id:
            raise ValueError("account pool not found")
        stmt = stmt.where(TgAccount.pool_id == pool.id)
    return list(session.scalars(stmt.order_by(TgAccount.id).offset((page - 1) * page_size).limit(page_size)))
