from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import or_, func, select
from sqlalchemy.orm import Session, object_session

from app.models import (
    AccountStatus,
    AccountProxy,
    AiProvider,
    AiProviderHealthStatus,
    Material,
    MaterialGroup,
    TgAccount,
    TgAccountAuthorization,
    TgAccountAuthorizationSnapshot,
    TgAccountDeviceCleanupPrecheck,
    TgAccountProfileBatchRule,
    TgAccountSecurityBatch,
    TgAccountSecurityBatchItem,
    TgAccountSecuritySnapshot,
    TgVerificationCode,
    TelegramDeveloperApp,
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
    ManagedTwoFaOut,
    ManagedTwoFaRevealOut,
    ManagedTwoFaRequest,
)
from app.security import decrypt_secret, encrypt_secret
from app.storage import media_root, object_path, save_avatar_bytes

from .._common import _now, ai_gateway, audit, gateway, require_tenant
from ..account_authorizations import attempt_standby_authorization_recovery, start_standby_authorization_login, verify_standby_authorization_login
from .device_classification import classify_account_authorization_snapshots, cleanup_candidate_authorization_snapshots
from ..account_two_fa import (
    MANAGED_TWO_FA_HINT,
    generate_managed_two_fa_password,
    managed_two_fa_password,
    record_managed_two_fa_password,
)
from ..ai_config import ai_provider_credentials, get_tenant_ai_setting
from ..developer_apps import credentials_for_account


USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
ASCII_LETTER_RE = re.compile(r"[A-Za-z]")
SYSTEM_DISPLAY_NAME_RE = re.compile(r"^导入\d{4}-\d{2,4}-\d{3}$")
GENERIC_DISPLAY_NAMES = {"", "托管账号", "新托管账号", "未命名账号"}
PROFILE_AI_BASE_TIMEOUT_SECONDS = 45
PROFILE_AI_MAX_TIMEOUT_SECONDS = 180
PROFILE_ACTIONS = {"update_profile", "update_username", "update_avatar"}
SECURITY_ACTIONS = {"cleanup_devices", "set_two_fa"}
STANDBY_SESSION_ACTIONS = {"provision_standby_session", "self_heal_session"}
ALL_ACTIONS = PROFILE_ACTIONS | SECURITY_ACTIONS | STANDBY_SESSION_ACTIONS
STANDBY_FAILURE_STATUS = {
    "verification_code_unreadable": "code_waiting",
    "two_fa_not_managed": "two_fa_waiting",
    "two_fa_invalid": "two_fa_waiting",
    "two_fa_rotation_failed": "two_fa_waiting",
    "standby_session_executor_missing": "manual_required",
}
STANDBY_CODE_POLL_INTERVAL_SECONDS = 2
STANDBY_CODE_POLL_FALLBACK_WINDOW_SECONDS = 20
DEVICE_CLEANUP_PRECHECK_TTL = timedelta(minutes=15)


@dataclass(frozen=True)
class ProfileUpdateValues:
    display_name: str
    first_name: str
    last_name: str
    bio: str
    replace_tg_name: bool
    replace_bio: bool


PROFILE_NAME_PREFIXES = [
    "锅巴",
    "蕉太",
    "早睡",
    "小熊",
    "香菜",
    "月亮",
    "西瓜",
    "糯米",
    "橘子",
    "云朵",
    "薄荷",
    "汽水",
    "青柠",
    "山竹",
    "奶盖",
    "小满",
    "晚风",
    "松弛",
    "芋泥",
    "海盐",
]
PROFILE_NAME_SUFFIXES = [
    "洋芋",
    "打烊",
    "便利店",
    "失败",
    "不加冰",
    "慢半拍",
    "看热闹",
    "路过中",
    "在摸鱼",
    "今天困",
    "小卖部",
    "备忘录",
    "散步中",
    "没电了",
    "等风来",
    "加点糖",
    "观察员",
    "碎碎念",
    "日记本",
    "开小差",
]
PROFILE_NAME_SHORTS = [
    "阿柚",
    "小葵",
    "山风",
    "七七",
    "橘白",
    "南星",
    "一栗",
    "鹿鹿",
    "木子",
    "小满",
    "青团",
    "半夏",
]
PROFILE_NAME_SCENES = [
    "凌晨三点还醒着",
    "便利店门口等雨停",
    "今天也没想好昵称",
    "薯条没有番茄酱",
    "路灯下面看消息",
    "周末不想出门",
    "把奶茶喝到见底",
    "在阳台吹晚风",
    "刚刚路过这里",
    "耳机里放小雨",
    "晚一点再回复",
    "先把热闹收藏",
]
PROFILE_NAME_PLAYFUL = [
    "不吃香菜",
    "早睡失败",
    "奶盖加满",
    "路过一下",
    "慢半拍中",
    "月亮打烊",
    "橘子汽水",
    "糯米团团",
    "薄荷小窗",
    "西瓜边角",
    "海盐日记",
    "芋泥备忘",
]
PROFILE_NAME_UNIQUE_TAILS = ["呢", "呀", "喔", "啦", "中", "了", "哈", "哦", "吧", "哇", "慢", "早", "晚", "轻", "小", "新", "晴", "雨", "风", "云"]
PROFILE_NAME_VARIANT_COUNT = 4
PROFILE_NAME_POOL_ACCOUNT_FACTOR = 5
PROFILE_NAME_POOL_INDEX_FACTOR = 6
PROFILE_NAME_VARIANT_ACCOUNT_FACTOR = 5
PROFILE_NAME_VARIANT_INDEX_FACTOR = 2
PROFILE_BIO_POOL = [
    "日常在线，随缘交流",
    "看到有意思的会回两句",
    "慢慢看，慢慢聊",
    "偶尔冒泡，不太正式",
    "记录一点生活碎片",
    "在线时间不固定，路过就看看",
    "喜欢新鲜事，也喜欢安静围观",
    "不赶时间，看到合适的话题会接一句",
    "今天也在认真看消息，偶尔分享小想法",
    "偏随缘的普通用户，熟一点就多聊两句",
    "有空会回，没空就先收藏着",
    "看见好玩的内容会停一下，顺手聊两句",
]
PROFILE_BIO_TAILS = [
    "",
    "。",
    "，先收藏。",
    "，有空再细看。",
    "，偶尔会冒泡。",
    "，不太爱刷屏。",
    "，看到合适的就接一句。",
    "，更多时候安静围观。",
    "，熟一点以后会多聊几句。",
    "，在线的时候回复比较快。",
]
PROFILE_USERNAME_WORDS = [
    "guoba",
    "banana",
    "sleepy",
    "bear",
    "mint",
    "moon",
    "melon",
    "mochi",
    "orange",
    "cloud",
    "soda",
    "lime",
    "mango",
    "milk",
    "yuni",
    "wind",
    "salt",
    "diary",
    "daily",
    "street",
    "snack",
    "corner",
    "garden",
    "bubble",
    "lofi",
]
PROFILE_USERNAME_TRAITS = [
    "note",
    "walk",
    "chat",
    "light",
    "fresh",
    "room",
    "day",
    "loop",
    "talk",
    "wave",
    "leaf",
    "box",
    "cup",
    "memo",
    "zone",
    "tiny",
    "mood",
    "ping",
    "spot",
    "line",
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
        "device_cleanup_precheck_id": item.device_cleanup_precheck_id,
        "two_fa_status": item.two_fa_status,
        "standby_session_status": _standby_session_status(item),
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


def _standby_session_status(item: TgAccountSecurityBatchItem) -> str:
    if item.failure_type in STANDBY_FAILURE_STATUS:
        return STANDBY_FAILURE_STATUS[item.failure_type]
    if item.status in {"pending", "running", "waiting", "failed", "succeeded", "manual_required"}:
        return item.status
    return "pending" if item.precheck_status == "pending" else item.precheck_status


def _require_account(session: Session, tenant_id: int, account_id: int) -> TgAccount:
    account = session.get(TgAccount, account_id)
    if not account or account.tenant_id != tenant_id or account.deleted_at is not None:
        raise ValueError("account not found")
    return account


def _managed_two_fa_out(account_id: int, snapshot: TgAccountSecuritySnapshot) -> ManagedTwoFaOut:
    return ManagedTwoFaOut(
        account_id=account_id,
        two_fa_status=snapshot.two_fa_status,
        password_stored_at=snapshot.two_fa_password_stored_at,
    )


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
    trusted = False
    for authorization in authorizations:
        is_current = bool(authorization.is_current)
        trusted = trusted or is_current
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
    session.flush()
    two_fa = gateway.get_two_fa_status(account.session_ciphertext, credentials)
    snapshot.trusted_session_status = "confirmed" if trusted else "missing"
    snapshot.two_fa_status = two_fa.status if two_fa.ok else "unknown"
    snapshot.external_authorization_count = len(cleanup_candidate_authorization_snapshots(session, account))
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


def create_device_cleanup_precheck(session: Session, tenant_id: int, account_id: int, actor: str) -> dict[str, object]:
    account = _cleanup_precheck_account(session, tenant_id, account_id)
    precheck = _create_device_cleanup_precheck_record(session, account, actor)
    session.commit()
    return _device_cleanup_precheck_out(precheck)


def _create_device_cleanup_precheck_record(session: Session, account: TgAccount, actor: str) -> TgAccountDeviceCleanupPrecheck:
    snapshots = _authorization_snapshots(session, account.id)
    if not snapshots:
        snapshot = refresh_account_security(session, account.tenant_id, account.id, actor=actor)
        snapshots = _authorization_snapshots(session, account.id)
        if not snapshots and snapshot.last_error:
            return _failed_device_cleanup_precheck(session, account, actor)
    missing_roles = _platform_slot_roles_missing_hash(session, account)
    if missing_roles:
        raise ValueError(f"平台授权设备 hash 未确认：{', '.join(missing_roles)}")
    candidates = cleanup_candidate_authorization_snapshots(session, account)
    cleanup_hashes = [snapshot.authorization_hash_ciphertext for snapshot in candidates if snapshot.authorization_hash_ciphertext]
    precheck = TgAccountDeviceCleanupPrecheck(
        precheck_id=f"device_cleanup_{uuid4().hex}",
        tenant_id=account.tenant_id,
        account_id=account.id,
        cleanup_authorization_hashes=json.dumps(cleanup_hashes),
        cleanup_count=len(cleanup_hashes),
        kept_count=_platform_authorization_count(session, account),
        unknown_count=_unknown_authorization_count(session, account),
        created_by=actor,
        expires_at=_now() + DEVICE_CLEANUP_PRECHECK_TTL,
    )
    session.add(precheck)
    session.flush()
    return precheck


def _failed_device_cleanup_precheck(session: Session, account: TgAccount, actor: str) -> TgAccountDeviceCleanupPrecheck:
    precheck = TgAccountDeviceCleanupPrecheck(
        precheck_id=f"device_cleanup_{uuid4().hex}",
        tenant_id=account.tenant_id,
        account_id=account.id,
        cleanup_authorization_hashes="[]",
        cleanup_count=0,
        kept_count=0,
        unknown_count=0,
        status="scan_failed",
        created_by=actor,
        expires_at=_now() + DEVICE_CLEANUP_PRECHECK_TTL,
    )
    session.add(precheck)
    session.flush()
    return precheck


def cleanup_devices_from_precheck(session: Session, tenant_id: int, account_id: int, precheck_id: str, actor: str) -> dict[str, object]:
    account = _cleanup_precheck_account(session, tenant_id, account_id)
    precheck = _device_cleanup_precheck(session, tenant_id, account.id, precheck_id)
    credentials = credentials_for_account(session, account)
    cleaned_count = 0
    failures: list[str] = []
    for encrypted_hash in _precheck_cleanup_hashes(precheck):
        raw_hash = decrypt_secret(encrypted_hash) or encrypted_hash
        result = gateway.cleanup_authorization(account.session_ciphertext, raw_hash, credentials)
        if result.ok:
            cleaned_count += 1
        else:
            failures.append(result.detail or result.failure_type)
    precheck.status = "succeeded" if not failures else "partial_success" if cleaned_count else "failed"
    precheck.confirmed_by = actor
    precheck.confirmed_at = _now()
    session.commit()
    return {
        **_device_cleanup_precheck_out(precheck),
        "cleaned_count": cleaned_count,
        "failed_count": len(failures),
        "failures": failures,
    }


def _cleanup_precheck_account(session: Session, tenant_id: int, account_id: int) -> TgAccount:
    account = session.get(TgAccount, account_id)
    if not account or account.tenant_id != tenant_id or account.deleted_at is not None:
        raise ValueError("account not found")
    if account.account_identity == "code_receiver":
        raise ValueError("接码专用账号禁止执行一键清理登录设备")
    return account


def _device_cleanup_precheck(session: Session, tenant_id: int, account_id: int, precheck_id: str) -> TgAccountDeviceCleanupPrecheck:
    precheck = session.scalar(
        select(TgAccountDeviceCleanupPrecheck).where(
            TgAccountDeviceCleanupPrecheck.tenant_id == tenant_id,
            TgAccountDeviceCleanupPrecheck.account_id == account_id,
            TgAccountDeviceCleanupPrecheck.precheck_id == precheck_id,
        )
    )
    if not precheck:
        raise ValueError("device cleanup precheck not found")
    if precheck.expires_at < _now():
        raise ValueError("device cleanup precheck expired")
    return precheck


def _precheck_cleanup_hashes(precheck: TgAccountDeviceCleanupPrecheck) -> list[str]:
    raw = json.loads(precheck.cleanup_authorization_hashes or "[]")
    return [str(value) for value in raw if value]


def _platform_authorization_count(session: Session, account: TgAccount) -> int:
    rows = classify_account_authorization_snapshots(session, account.id)
    return sum(1 for row in rows if row.get("classification") == "platform_app")


def _unknown_authorization_count(session: Session, account: TgAccount) -> int:
    rows = classify_account_authorization_snapshots(session, account.id)
    return sum(1 for row in rows if row.get("classification") == "unknown")


def _device_cleanup_precheck_out(precheck: TgAccountDeviceCleanupPrecheck) -> dict[str, object]:
    classified = _device_cleanup_precheck_devices(session=object_session(precheck), precheck=precheck)
    return {
        "precheck_id": precheck.precheck_id,
        "account_id": precheck.account_id,
        "cleanup_count": precheck.cleanup_count,
        "kept_count": precheck.kept_count,
        "unknown_count": precheck.unknown_count,
        "kept_devices": classified["kept_devices"],
        "cleanup_devices": classified["cleanup_devices"],
        "unknown_devices": classified["unknown_devices"],
        "status": precheck.status,
        "expires_at": precheck.expires_at,
    }


def _device_cleanup_precheck_devices(session: Session | None, precheck: TgAccountDeviceCleanupPrecheck) -> dict[str, list[dict[str, object]]]:
    if session is None:
        return {"kept_devices": [], "cleanup_devices": [], "unknown_devices": []}
    cleanup_hashes = set(_precheck_cleanup_hashes(precheck))
    devices = classify_account_authorization_snapshots(session, precheck.account_id)
    result = {"kept_devices": [], "cleanup_devices": [], "unknown_devices": []}
    for item in devices:
        device = _device_cleanup_precheck_device_out(item)
        if item.get("classification") == "unknown":
            result["unknown_devices"].append(device)
        elif _snapshot_hash_in_precheck(session, int(item["id"]), cleanup_hashes):
            result["cleanup_devices"].append(device)
        else:
            result["kept_devices"].append(device)
    return result


def _device_cleanup_precheck_device_out(item: dict[str, object]) -> dict[str, object]:
    return {
        "id": item.get("id"),
        "app_name": item.get("app_name"),
        "device_model": item.get("device_model"),
        "platform": item.get("platform"),
        "remote_api_id": item.get("remote_api_id"),
        "classification": item.get("classification"),
        "matched_roles": item.get("matched_roles", []),
        "cleanup_eligible": item.get("cleanup_eligible", False),
    }


def _snapshot_hash_in_precheck(session: Session, snapshot_id: int, cleanup_hashes: set[str]) -> bool:
    snapshot = session.get(TgAccountAuthorizationSnapshot, snapshot_id)
    if not snapshot:
        return False
    encrypted_hash = snapshot.authorization_hash_ciphertext
    raw_hash = decrypt_secret(encrypted_hash) or encrypted_hash
    return bool(raw_hash and (raw_hash in cleanup_hashes or encrypted_hash in cleanup_hashes))


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
    authorization_rows = list(
        session.scalars(
            select(TgAccountAuthorizationSnapshot)
            .where(TgAccountAuthorizationSnapshot.tenant_id == tenant_id, TgAccountAuthorizationSnapshot.account_id == account_id)
            .order_by(TgAccountAuthorizationSnapshot.is_current_session.desc(), TgAccountAuthorizationSnapshot.id.desc())
        )
    )
    classifications = {item["id"]: item for item in classify_account_authorization_snapshots(session, account_id)}
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
        authorizations=[_authorization_snapshot_out(row, classifications.get(row.id, {})) for row in authorization_rows],
        recent_batches=[_batch_out(batch) for batch in batches],
    )


def _authorization_snapshot_out(row: TgAccountAuthorizationSnapshot, classification: dict[str, object]) -> dict[str, object]:
    return {
        "id": row.id,
        "account_id": row.account_id,
        "batch_id": row.batch_id,
        "authorization_hash_ciphertext": row.authorization_hash_ciphertext,
        "is_platform_trusted": row.is_platform_trusted,
        "is_current_session": row.is_current_session,
        "device_model": row.device_model,
        "platform": row.platform,
        "system_version": row.system_version,
        "api_id": row.api_id,
        "app_name": row.app_name,
        "app_version": row.app_version,
        "ip_masked": row.ip_masked,
        "country": row.country,
        "region": row.region,
        "date_created": row.date_created,
        "date_active": row.date_active,
        "status": row.status,
        "scanned_at": row.scanned_at,
        "classification": classification.get("classification", "unknown"),
        "matched_roles": classification.get("matched_roles", []),
        "cleanup_eligible": classification.get("cleanup_eligible", False),
    }


def save_managed_two_fa_password(
    session: Session,
    tenant_id: int,
    account_id: int,
    payload: ManagedTwoFaRequest,
    actor: str,
) -> ManagedTwoFaOut:
    account = _require_account(session, tenant_id, account_id)
    snapshot = record_managed_two_fa_password(session, account, payload.password)
    audit(session, tenant_id=tenant_id, actor=actor, action="保存账号托管二步密码", target_type="tg_account", target_id=str(account.id), detail=payload.reason)
    session.commit()
    return _managed_two_fa_out(account.id, snapshot)


def rotate_managed_two_fa_password(
    session: Session,
    tenant_id: int,
    account_id: int,
    payload: ManagedTwoFaRequest,
    actor: str,
) -> ManagedTwoFaOut:
    account = _require_account(session, tenant_id, account_id)
    credentials = credentials_for_account(session, account)
    result = gateway.set_two_fa_password(
        account.session_ciphertext,
        payload.password,
        credentials=credentials,
        hint=MANAGED_TWO_FA_HINT,
        current_password=managed_two_fa_password(session, account),
    )
    if not result.ok:
        raise ValueError(result.detail or result.failure_type or "2FA rotate failed")
    snapshot = record_managed_two_fa_password(session, account, payload.password)
    audit(session, tenant_id=tenant_id, actor=actor, action="轮换账号托管二步密码", target_type="tg_account", target_id=str(account.id), detail=payload.reason)
    session.commit()
    return _managed_two_fa_out(account.id, snapshot)


def reveal_managed_two_fa_password(
    session: Session,
    tenant_id: int,
    account_id: int,
    actor: str,
) -> ManagedTwoFaRevealOut:
    account = _require_account(session, tenant_id, account_id)
    snapshot = session.scalar(select(TgAccountSecuritySnapshot).where(TgAccountSecuritySnapshot.account_id == account.id))
    if not snapshot or not snapshot.two_fa_password_ciphertext:
        raise ValueError("托管 2FA 密码未配置")
    audit(session, tenant_id=tenant_id, actor=actor, action="查看账号托管二步密码", target_type="tg_account", target_id=str(account.id))
    revealed_at = _now()
    password = decrypt_secret(snapshot.two_fa_password_ciphertext)
    session.commit()
    return ManagedTwoFaRevealOut(account_id=account.id, password=password, revealed_at=revealed_at)


def precheck_account_security_batch(session: Session, tenant_id: int, payload: AccountSecurityPrecheckRequest) -> AccountSecurityPrecheckOut:
    require_tenant(session, tenant_id)
    action_types = _valid_actions(payload.action_types)
    accounts = _accounts_for_payload(session, tenant_id, payload.account_ids)
    trace_id = uuid4().hex
    items: list[AccountSecurityPreviewItem] = []
    needs_profile_preview = bool(set(action_types) & PROFILE_ACTIONS)
    overrides = {override.account_id: override for override in payload.preview_overrides}
    if needs_profile_preview:
        accounts_needing_generation = [account for account in accounts if account.id not in overrides]
        generated_by_id = {
            account.id: generated_item
            for account, generated_item in zip(
                accounts_needing_generation,
                _generate_profiles(session, tenant_id, accounts_needing_generation, payload.profile_strategy) if accounts_needing_generation else [],
            )
        }
        generated = [
            _account_profile_preview(account) if account.id in overrides else generated_by_id[account.id]
            for account in accounts
        ]
    else:
        generated = [_account_profile_preview(account) for account in accounts]
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
        can_self_heal = "self_heal_session" in action_types and _has_switchable_standby(session, account)
        if (account.status != AccountStatus.ACTIVE.value or not account.session_ciphertext) and not can_self_heal:
            blockers.append("账号未在线或缺少可用 session")
            suggested.append("已自动跳过；处理登录或 session 后可重新发起")
            status = "skipped"
        wait_until = _fresh_session_wait_until(session, account) if "cleanup_devices" in action_types else None
        if wait_until:
            blockers.append(f"新登录 Session 未满 24 小时，需等待到 {wait_until.isoformat()}")
            suggested.append("等待 Telegram 安全限制解除后重试设备清理")
            status = "waiting"
        if "set_two_fa" in action_types and snapshot.two_fa_status == "enabled":
            warnings.append("账号已设置二步验证，将跳过 2FA 设置")
        if "provision_standby_session" in action_types and account.status == AccountStatus.ACTIVE.value and account.session_ciphertext:
            standby_status = _standby_precheck_status(session, account, payload.standby_slot_strategy)
            blockers.extend(standby_status.blockers)
            warnings.extend(standby_status.warnings)
            suggested.extend(standby_status.suggested_actions)
            if standby_status.blockers and status == "executable":
                status = "manual_required"
        override = overrides.get(account.id)
        generated_item = _apply_preview_override(generated[index], override)
        if generated_item.get("generation_error"):
            message = str(generated_item["generation_error"])
            if override:
                warnings.append(f"{message}；已使用本次手工编辑预览")
            else:
                blockers.append(message)
                suggested.append("已自动跳过；切换模板兜底、导入名单，或手工编辑后重新预检")
                status = "skipped"
        if generated_item.get("generation_warning"):
            warnings.append(str(generated_item["generation_warning"]))
        username_candidates = generated_item["username_candidates"] if "update_username" in action_types else []
        invalid_usernames = [candidate for candidate in username_candidates if not USERNAME_RE.match(candidate)]
        if invalid_usernames:
            blockers.append(f"username 候选格式错误：{','.join(invalid_usernames)}")
            suggested.append("已自动跳过；修正 username 候选后可重新发起")
            status = "skipped"
        avatar_source = str(generated_item.get("avatar_source") or _avatar_source(session, account, index, payload.avatar_strategy))
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
                phone_number=account.phone_number,
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
    confirmed = _is_batch_confirmed(payload.confirm_text)
    initial_status = "running" if confirmed and preview.summary.get("executable", 0) > 0 else "ready"
    profile_strategy_payload = payload.profile_strategy.model_dump(mode="json")
    profile_strategy_payload["standby_slot_strategy"] = payload.standby_slot_strategy
    batch = TgAccountSecurityBatch(
        tenant_id=tenant_id,
        action_types=json.dumps(preview.action_types, ensure_ascii=False),
        status=initial_status,
        total_count=len(preview.items),
        created_by=actor,
        confirmed_by=actor if confirmed else "",
        confirm_text=payload.confirm_text,
        password_strategy=payload.password_strategy,
        profile_strategy=json.dumps(profile_strategy_payload, ensure_ascii=False),
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
        cleanup_precheck_id = ""
        if "cleanup_devices" in preview.action_types and item_status == "pending":
            account = _require_account(session, tenant_id, preview_item.account_id)
            cleanup_precheck_id = _create_device_cleanup_precheck_record(session, account, actor).precheck_id
        item = TgAccountSecurityBatchItem(
            batch_id=batch.id,
            tenant_id=tenant_id,
            account_id=preview_item.account_id,
            status=item_status,
            precheck_status=preview_item.precheck_status,
            cleanup_status="pending" if "cleanup_devices" in preview.action_types and item_status == "pending" else "not_requested",
            device_cleanup_precheck_id=cleanup_precheck_id,
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
    session.flush()
    _refresh_batch_counts(session, batch)
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
        credentials = None
        needs_account_credentials = bool(action_types & (SECURITY_ACTIONS | PROFILE_ACTIONS))
        if needs_account_credentials:
            credentials = credentials_for_account(session, account)
        if "cleanup_devices" in action_types:
            failures.extend(_execute_cleanup(session, account, item, credentials))
        if "set_two_fa" in action_types:
            generated_password = generate_managed_two_fa_password(account, str(item.id))
            result = gateway.set_two_fa_password(
                account.session_ciphertext,
                generated_password,
                credentials=credentials,
                hint=MANAGED_TWO_FA_HINT,
                current_password=managed_two_fa_password(session, account),
            )
            item.two_fa_status = result.status if result.ok else "failed"
            if not result.ok:
                failures.append(result.detail or result.failure_type)
            else:
                record_managed_two_fa_password(session, account, generated_password)
        if action_types & STANDBY_SESSION_ACTIONS:
            failures.extend(_execute_standby_session_provision(session, account, item, action_types))
        if "update_profile" in action_types and item.profile_status not in {"succeeded", "skipped"}:
            profile_values = _profile_update_values(account, item, overwrite_existing=batch.overwrite_existing_profile)
            profile_result = gateway.update_profile(
                account.session_ciphertext,
                first_name=profile_values.first_name or profile_values.display_name or account.display_name,
                last_name=profile_values.last_name,
                bio=profile_values.bio,
                credentials=credentials,
            )
            item.profile_status = "succeeded" if profile_result.ok else "failed"
            if profile_result.ok:
                account.display_name = profile_values.display_name or account.display_name
                if profile_values.replace_tg_name:
                    account.tg_first_name = profile_values.first_name or profile_values.display_name
                    account.tg_last_name = profile_values.last_name
                if profile_values.replace_bio:
                    account.tg_bio = profile_values.bio
                account.profile_sync_status = "已同步"
                account.profile_sync_error = ""
                account.profile_synced_at = _now()
            else:
                failures.append(profile_result.detail)
        if "update_username" in action_types and item.username_status not in {"succeeded", "skipped"}:
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
        if "update_avatar" in action_types and item.avatar_status not in {"succeeded", "skipped"}:
            avatar_result = _execute_avatar_update(session, account, item, credentials, overwrite_existing=batch.overwrite_existing_profile)
            item.avatar_status = avatar_result.status or ("skipped" if avatar_result.ok else "failed")
            if not avatar_result.ok and avatar_result.status != "skipped":
                failures.append(avatar_result.detail or avatar_result.failure_type)
        snapshot = _snapshot(session, account)
        snapshot.profile_status = _profile_status(account)
        snapshot.last_hardened_at = _now()
        snapshot.last_error = ";".join(failures)
        if item.status != "manual_required":
            item.status = "waiting" if _item_should_wait(item, failures) else "partial_success" if failures and _item_has_success(item) else "failed" if failures else "succeeded"
        item.failure_type = _item_failure_type(item, failures)
        item.failure_detail = ";".join(failures)
    except Exception as exc:  # noqa: BLE001 - operator-facing batch failure.
        item.status = "failed"
        item.failure_type = "执行异常"
        item.failure_detail = str(exc)
    item.finished_at = _now()
    _refresh_batch_counts(session, batch)
    audit(session, tenant_id=batch.tenant_id, actor="account-security-worker", action="执行账号安全加固项", target_type="account_security_batch_item", target_id=str(item.id), detail=item.status)
    session.commit()


def _execute_standby_session_provision(
    session: Session,
    account: TgAccount,
    item: TgAccountSecurityBatchItem,
    action_types: set[str],
) -> list[str]:
    if "self_heal_session" in action_types:
        recovered = attempt_standby_authorization_recovery(
            session,
            account,
            actor="account-security-worker",
            reason="账号安全批次触发备用 session 自愈恢复",
        )
        if recovered is not None:
            item.failure_type = ""
            item.failure_detail = f"已从 {recovered.role} 激活恢复"
            return []
    if "provision_standby_session" in action_types:
        provision_failure = _auto_provision_standby_session(session, account, item)
        if not provision_failure:
            return []
        item.failure_detail = provision_failure
    item.status = "manual_required"
    item.failure_type = item.failure_type or "standby_session_executor_missing"
    item.failure_detail = item.failure_detail or "备用 session 自动补齐执行器尚未接入登录网关"
    return [item.failure_detail]


def _auto_provision_standby_session(session: Session, account: TgAccount, item: TgAccountSecurityBatchItem) -> str:
    if not account.session_ciphertext:
        item.failure_type = "account_not_online"
        return "主 session 不可用，无法自动读取备用登录验证码"
    roles = _target_standby_roles(session, account, item)
    if not roles:
        item.failure_detail = "备用 session 已满足一主两从"
        return ""
    app = _auto_standby_developer_app(session, account)
    if app is None:
        item.failure_type = "developer_app_unavailable"
        return "没有可用的 TG 开发者应用用于备用 session 登录"
    proxy = _auto_standby_proxy(session, account)
    if proxy is None:
        item.failure_type = "proxy_unavailable"
        return "没有可用代理用于备用 session 登录"
    password_2fa = managed_two_fa_password(session, account)
    completed_roles: list[str] = []
    for role in roles:
        failure = _auto_provision_standby_role(session, account, item, role, app, proxy, password_2fa)
        if failure:
            return failure
        completed_roles.append(role)
        password_2fa = managed_two_fa_password(session, account) or password_2fa
    item.failure_type = ""
    item.failure_detail = f"已补齐 {', '.join(completed_roles)} 备用 session"
    return ""


def _auto_provision_standby_role(
    session: Session,
    account: TgAccount,
    item: TgAccountSecurityBatchItem,
    role: str,
    app: TelegramDeveloperApp,
    proxy: AccountProxy,
    password_2fa: str | None,
) -> str:
    flow = start_standby_authorization_login(
        session,
        account.id,
        method="code",
        role=role,
        developer_app_id=app.id,
        proxy_id=proxy.id,
        actor="account-security-worker",
    )
    code = _standby_login_code(session, account, flow)
    if not code:
        item.failure_type = "verification_code_unreadable"
        return "验证码不可读取，已记录备用授权登录流水"
    try:
        asset = verify_standby_authorization_login(
            session,
            account.id,
            flow.id,
            code=code,
            password_2fa=password_2fa,
            actor="account-security-worker",
        )
    except ValueError as exc:
        item.failure_type = _standby_login_failure_type(str(exc), password_2fa)
        return _standby_login_failure_detail(item.failure_type, str(exc))
    item.failure_detail = f"已补齐 {asset.role} 备用 session"
    return ""


def _standby_login_failure_type(detail: str, password_2fa: str | None) -> str:
    if "修改为平台托管新密码失败" in detail:
        return "two_fa_rotation_failed"
    if AccountStatus.WAITING_2FA.value in detail:
        return "two_fa_invalid" if password_2fa else "two_fa_not_managed"
    return "standby_login_failed"


def _standby_login_failure_detail(failure_type: str, detail: str) -> str:
    if failure_type == "two_fa_not_managed":
        return "Telegram 要求二步密码，但账号未托管 2FA"
    if failure_type == "two_fa_invalid":
        return "托管 2FA 校验失败，需轮换托管 2FA 后重试"
    if failure_type == "two_fa_rotation_failed":
        return f"备用登录已完成，但 2FA 新密码轮换失败：{detail}"
    return detail


def _standby_login_code(session: Session, account: TgAccount, flow) -> str | None:
    if flow.code_preview:
        return flow.code_preview
    deadline = flow.code_expires_at or (_now() + timedelta(seconds=STANDBY_CODE_POLL_FALLBACK_WINDOW_SECONDS))
    while _now() <= deadline:
        code = _poll_standby_login_code_once(session, account, flow)
        if code:
            return code
        time.sleep(STANDBY_CODE_POLL_INTERVAL_SECONDS)
    return None


def _poll_standby_login_code_once(session: Session, account: TgAccount, flow) -> str | None:
    try:
        snapshots = gateway.poll_verification_codes(
            account.id,
            session_ciphertext=account.session_ciphertext,
            credentials=credentials_for_account(session, account),
        )
    except Exception as exc:
        flow.failure_type = "verification_code_poll_failed"
        flow.failure_detail = str(exc)
        return None
    if not snapshots:
        return None
    snapshot = snapshots[0]
    flow.code_preview = snapshot.code
    flow.code_expires_at = snapshot.expires_at
    session.add(
        TgVerificationCode(
            tenant_id=account.tenant_id,
            account_id=account.id,
            source="standby_authorization_auto_login",
            code_preview=snapshot.code,
            expires_at=snapshot.expires_at,
            raw_hint=snapshot.raw_hint,
        )
    )
    return snapshot.code


def _execute_cleanup(session: Session, account: TgAccount, item: TgAccountSecurityBatchItem, credentials) -> list[str]:
    if account.account_identity == "code_receiver":
        item.status = "manual_required"
        item.cleanup_status = "manual_required"
        item.failure_type = "code_receiver_reserved"
        return ["接码专用账号禁止执行一键清理登录设备"]
    if not item.device_cleanup_precheck_id:
        item.cleanup_status = "failed"
        item.failure_type = "device_cleanup_precheck_missing"
        return ["缺少设备清理预检快照，禁止现场重新扫描后清理"]
    precheck = _device_cleanup_precheck(session, account.tenant_id, account.id, item.device_cleanup_precheck_id)
    if precheck.status == "scan_failed":
        item.cleanup_status = "failed"
        item.failure_type = "device_cleanup_scan_failed"
        return [_device_cleanup_scan_failure_detail(session, account)]
    failures: list[str] = []
    cleanup_hashes = _precheck_cleanup_hashes(precheck)
    item.external_devices_before = len(cleanup_hashes)
    cleaned = 0
    waiting_for_fresh_reset = False
    for encrypted_hash in cleanup_hashes:
        raw_hash = decrypt_secret(encrypted_hash) or encrypted_hash
        result = gateway.cleanup_authorization(account.session_ciphertext, raw_hash, credentials)
        if result.ok:
            cleaned += 1
        else:
            if _is_fresh_reset_forbidden(result.detail or result.failure_type):
                item.next_retry_at = _now() + timedelta(hours=24)
                item.cleanup_status = "waiting"
                item.status = "waiting"
                waiting_for_fresh_reset = True
            failures.append(result.detail or result.failure_type)
    item.external_devices_after = max(0, len(cleanup_hashes) - cleaned)
    if not waiting_for_fresh_reset or not failures:
        item.cleanup_status = "succeeded" if not failures else "partial_success" if cleaned else "failed"
    precheck.status = item.cleanup_status
    precheck.confirmed_by = "account-security-worker"
    precheck.confirmed_at = _now()
    snapshot = _snapshot(session, account)
    snapshot.external_authorization_count = item.external_devices_after
    snapshot.last_device_scan_at = _now()
    return failures


def _device_cleanup_scan_failure_detail(session: Session, account: TgAccount) -> str:
    snapshot = _snapshot(session, account)
    detail = snapshot.last_error or "未知错误"
    return f"设备扫描失败：{detail}"


def _authorization_snapshots(session: Session, account_id: int) -> list[TgAccountAuthorizationSnapshot]:
    return list(
        session.scalars(
            select(TgAccountAuthorizationSnapshot).where(TgAccountAuthorizationSnapshot.account_id == account_id)
        )
    )


def _has_protected_hash(authorization: TgAccountAuthorizationSnapshot, protected_hashes: set[str]) -> bool:
    raw_hash = decrypt_secret(authorization.authorization_hash_ciphertext) or authorization.authorization_hash_ciphertext
    return bool(raw_hash and raw_hash in protected_hashes)


def _protected_authorization_hashes(session: Session, account: TgAccount) -> set[str]:
    rows = session.scalars(
        select(TgAccountAuthorization).where(
            TgAccountAuthorization.account_id == account.id,
            TgAccountAuthorization.disabled_at.is_(None),
            TgAccountAuthorization.telegram_authorization_hash_ciphertext != "",
        )
    )
    return {
        value
        for row in rows
        if row.role in {"primary", "standby_1", "standby_2"} or row.is_current
        for value in [decrypt_secret(row.telegram_authorization_hash_ciphertext) or row.telegram_authorization_hash_ciphertext]
        if value
    }


def _platform_slot_roles_missing_hash(session: Session, account: TgAccount) -> list[str]:
    rows = session.scalars(
        select(TgAccountAuthorization).where(
            TgAccountAuthorization.account_id == account.id,
            TgAccountAuthorization.disabled_at.is_(None),
            TgAccountAuthorization.role.in_(["primary", "standby_1", "standby_2"]),
            TgAccountAuthorization.session_ciphertext != "",
        )
    )
    return [
        row.role
        for row in rows
        if not _usable_authorization_hash(decrypt_secret(row.telegram_authorization_hash_ciphertext) or row.telegram_authorization_hash_ciphertext)
    ]


def _usable_authorization_hash(value: str | None) -> bool:
    raw = str(value or "").strip()
    return raw not in {"", "0"}


def _has_switchable_standby(session: Session, account: TgAccount) -> bool:
    return bool(
        session.scalar(
            select(TgAccountAuthorization.id)
            .where(
                TgAccountAuthorization.account_id == account.id,
                TgAccountAuthorization.disabled_at.is_(None),
                TgAccountAuthorization.role.in_(["standby_1", "standby_2"]),
                TgAccountAuthorization.status.in_(["active", "standby"]),
                TgAccountAuthorization.session_ciphertext != "",
            )
            .limit(1)
        )
    )


def _target_standby_role(session: Session, account: TgAccount, item: TgAccountSecurityBatchItem) -> str:
    return _target_standby_role_for_strategy(session, account, _standby_slot_strategy(session, item))


def _target_standby_roles(session: Session, account: TgAccount, item: TgAccountSecurityBatchItem) -> list[str]:
    return _target_standby_roles_for_strategy(session, account, _standby_slot_strategy(session, item))


def _target_standby_role_for_strategy(session: Session, account: TgAccount, requested: str) -> str:
    roles = _target_standby_roles_for_strategy(session, account, requested)
    return roles[0] if roles else ""


def _target_standby_roles_for_strategy(session: Session, account: TgAccount, requested: str) -> list[str]:
    existing_roles = set(
        session.scalars(
            select(TgAccountAuthorization.role).where(
                TgAccountAuthorization.account_id == account.id,
                TgAccountAuthorization.disabled_at.is_(None),
                TgAccountAuthorization.role.in_(["standby_1", "standby_2"]),
                TgAccountAuthorization.status.in_(["active", "standby"]),
                TgAccountAuthorization.session_ciphertext != "",
            )
        )
    )
    if requested in {"standby_1", "standby_2"}:
        return [] if requested in existing_roles else [requested]
    return [role for role in ["standby_1", "standby_2"] if role not in existing_roles]


def _standby_slot_strategy(session: Session, item: TgAccountSecurityBatchItem) -> str:
    batch = session.get(TgAccountSecurityBatch, item.batch_id)
    strategy = _json_dict(batch.profile_strategy).get("standby_slot_strategy") if batch else ""
    return str(strategy or "auto")


@dataclass(frozen=True)
class StandbyPrecheckStatus:
    blockers: list[str]
    warnings: list[str]
    suggested_actions: list[str]


def _standby_precheck_status(session: Session, account: TgAccount, standby_slot_strategy: str) -> StandbyPrecheckStatus:
    blockers: list[str] = []
    warnings: list[str] = []
    suggested: list[str] = []
    roles = _target_standby_roles_for_strategy(session, account, standby_slot_strategy)
    if not roles:
        warnings.append("备用 session 已满足一主两从")
        return StandbyPrecheckStatus(blockers, warnings, suggested)
    if _auto_standby_developer_app(session, account) is None:
        blockers.append("没有可用的 TG 开发者应用用于备用 session 登录")
        suggested.append("先在系统配置中补充健康 TG 开发者应用")
    if _auto_standby_proxy(session, account) is None:
        blockers.append("没有可用代理用于备用 session 登录")
        suggested.append("先绑定或补充健康账号代理")
    if not managed_two_fa_password(session, account):
        warnings.append("账号未托管 2FA；如果 Telegram 要求二步密码，备用 session 补齐会进入人工处理")
    return StandbyPrecheckStatus(blockers, warnings, suggested)


def _auto_standby_developer_app(session: Session, account: TgAccount) -> TelegramDeveloperApp | None:
    stmt = (
        select(TelegramDeveloperApp)
        .where(
            TelegramDeveloperApp.is_active.is_(True),
            TelegramDeveloperApp.health_status == "健康",
        )
        .order_by((TelegramDeveloperApp.id == account.developer_app_id).asc(), TelegramDeveloperApp.id.asc())
        .limit(1)
    )
    return session.scalar(stmt)


def _auto_standby_proxy(session: Session, account: TgAccount) -> AccountProxy | None:
    stmt = (
        select(AccountProxy)
        .where(
            AccountProxy.tenant_id == account.tenant_id,
            AccountProxy.status.in_(["healthy", "健康"]),
            AccountProxy.alert_status.in_(["normal", "recovered", ""]),
        )
        .order_by((AccountProxy.id == account.proxy_id).asc(), AccountProxy.id.asc())
        .limit(1)
    )
    return session.scalar(stmt)


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


def _execute_avatar_update(session: Session, account: TgAccount, item: TgAccountSecurityBatchItem, credentials, *, overwrite_existing: bool = False) -> object:
    if account.avatar_object_key and not overwrite_existing:
        return SimpleNamespace(ok=True, status="skipped", detail="账号已有头像，未开启覆盖", failure_type="")
    if not item.avatar_source:
        return SimpleNamespace(ok=True, status="skipped", detail="未配置头像来源", failure_type="")
    cache_failure = _avatar_cache_failure_reason(session, item.avatar_source)
    if cache_failure:
        return SimpleNamespace(ok=False, status="failed", detail=cache_failure, failure_type="material_cache_failed")
    cache_error = _avatar_cache_wait_reason(session, item.avatar_source)
    if cache_error:
        item.next_retry_at = _now() + timedelta(minutes=1)
        return SimpleNamespace(ok=False, status="waiting_cache", detail=cache_error, failure_type="waiting_material_cache")
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


def _item_failure_type(item: TgAccountSecurityBatchItem, failures: list[str]) -> str:
    if not failures:
        return ""
    if item.avatar_status == "waiting_cache":
        return "waiting_material_cache"
    if item.status == "manual_required":
        return item.failure_type or "manual_required"
    if item.status == "waiting":
        return "需等待"
    if item.status == "partial_success":
        return "部分失败"
    return "执行失败"


def _item_should_wait(item: TgAccountSecurityBatchItem, failures: list[str]) -> bool:
    if not failures or not item.next_retry_at:
        return False
    if item.avatar_status == "waiting_cache":
        return True
    return not _item_has_success(item)


def _avatar_cache_failure_reason(session: Session, source: str) -> str:
    material = _material_for_avatar_source(session, source)
    if not material:
        return ""
    if material.cache_ready_status == "cache_failed":
        return "头像素材 TG 缓存失败"
    if material.cache_ready_status == "ready" and not (material.tg_cache_peer_id and material.tg_cache_message_id):
        return "头像素材 TG 缓存引用不完整"
    return ""


def _avatar_cache_wait_reason(session: Session, source: str) -> str:
    material = _material_for_avatar_source(session, source)
    if not material or material.cache_ready_status == "ready":
        return ""
    return f"头像素材 TG 缓存未完成：{material.cache_ready_status or 'not_cached'}"


def _material_for_avatar_source(session: Session, source: str) -> Material | None:
    value = (source or "").strip()
    if value.startswith("avatar:"):
        value = value.removeprefix("avatar:")
    if not (value.startswith("material:") or value.isdigit()):
        return None
    try:
        material_id = int(value.removeprefix("material:"))
    except ValueError:
        return None
    return session.get(Material, material_id)


def _resolve_avatar_source(session: Session, account: TgAccount, source: str) -> tuple[Path, str]:
    value = (source or "").strip()
    if not value:
        raise ValueError("头像来源为空")
    if value.startswith("avatar:"):
        value = value.removeprefix("avatar:")
    if value.startswith("material:") or value.isdigit():
        return _resolve_material_avatar_source(session, account, value, source)
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


def _resolve_material_avatar_source(session: Session, account: TgAccount, value: str, source: str) -> tuple[Path, str]:
    try:
        material_id = int(value.removeprefix("material:"))
    except ValueError as exc:
        raise ValueError("头像素材 ID 格式不正确") from exc
    material = session.get(Material, material_id)
    if not material or material.tenant_id != account.tenant_id:
        raise ValueError("头像素材不存在或不属于当前租户")
    if _avatar_cache_wait_reason(session, source):
        raise ValueError("头像素材 TG 缓存未完成")
    if material.material_type != "图片":
        raise ValueError("头像素材必须是图片类型")
    data = _material_avatar_bytes(session, material)
    object_key, avatar_path = save_avatar_bytes(
        tenant_id=account.tenant_id,
        account_id=account.id,
        content_type=material.mime_type or "image/jpeg",
        data=data,
    )
    return avatar_path, object_key


def _material_avatar_bytes(session: Session, material: Material) -> bytes:
    if material.content:
        source_path = Path(material.content)
        if source_path.exists() and source_path.is_file():
            return source_path.read_bytes()
    if not material.tg_cache_account_id:
        raise ValueError("头像素材缺少 TG 缓存账号")
    cache_account = session.get(TgAccount, material.tg_cache_account_id)
    if not cache_account:
        raise ValueError("头像素材 TG 缓存账号不存在")
    credentials = credentials_for_account(session, cache_account)
    result = gateway.download_cached_material(
        cache_account.id,
        material.tg_cache_peer_id,
        material.tg_cache_message_id,
        cache_account.session_ciphertext,
        credentials,
    )
    if not result.ok or not result.data:
        raise ValueError(result.detail or result.failure_type or "头像素材 TG 缓存下载失败")
    return result.data


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
        if material.cache_ready_status == "cache_failed":
            return "头像素材 TG 缓存失败"
        if material.cache_ready_status == "ready":
            if material.tg_cache_peer_id and material.tg_cache_message_id:
                return ""
            return "头像素材 TG 缓存引用不完整"
        if material.content and Path(material.content).exists():
            return ""
        if material.cache_ready_status in {"not_cached", "refreshing", "flood_wait"}:
            return "头像素材等待 TG 缓存，但缺少可缓存的本地来源"
        return "头像素材文件不存在"
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
    elif batch.success_count:
        batch.status = "succeeded"
    elif batch.skipped_count:
        batch.status = "manual_required"
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
    fields_set = getattr(override, "model_fields_set", set())
    if "generated_display_name" in fields_set and override.generated_display_name:
        item["display_name"] = override.generated_display_name
    if "generated_first_name" in fields_set:
        item["first_name"] = override.generated_first_name
    if "generated_last_name" in fields_set:
        item["last_name"] = override.generated_last_name
    if "generated_bio" in fields_set:
        item["bio"] = override.generated_bio
    if "avatar_source" in fields_set and override.avatar_source:
        item["avatar_source"] = override.avatar_source
    if "username_candidates" in fields_set:
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
        "展示资料语言要求：display_name、first_name、last_name、bio 禁止出现英文字母；不要生成“中文昵称 + 英文长尾”或英文签名，英文只允许出现在 username_candidates。\n"
        "整批差异要求：不要使用同一命名公式、同一字数、同一简介句式或同一 username 前缀批量套壳；"
        "至少混合短昵称、长一点的生活化昵称、无厘头昵称、食物/心情/场景类昵称；简介长度也要有明显差异。\n"
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
        last_name = _clean_ai_profile_last_name(raw_item.get("last_name"))
        bio = str(raw_item.get("bio") or "").strip()[:160] if strategy.bio_enabled else ""
        candidates_raw = raw_item.get("username_candidates") or []
        candidates = [str(candidate).strip().lstrip("@") for candidate in candidates_raw if isinstance(candidate, str)]
        candidates = [candidate for candidate in candidates if USERNAME_RE.match(candidate)]
        candidates = [candidate for candidate in candidates if candidate.lower() not in used_usernames]
        if not display_name or display_name in used_names:
            continue
        if _has_profile_ascii_text(display_name, first_name, bio):
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


def _clean_ai_profile_last_name(value: object) -> str:
    text = str(value or "").strip()[:80]
    if ASCII_LETTER_RE.search(text):
        return ""
    return text


def _has_profile_ascii_text(*values: str) -> bool:
    return any(ASCII_LETTER_RE.search(value or "") for value in values)


def _generate_profiles_from_local_pool(accounts: list[TgAccount], strategy) -> list[dict[str, object]]:
    forbidden = {word.strip() for word in strategy.forbidden_words if word.strip()}
    results: list[dict[str, object]] = []
    used_names: set[str] = set()
    for index, account in enumerate(accounts):
        display_name = _unique_local_profile_display_name(_local_profile_display_name(account.id, index), used_names)
        used_names.add(display_name)
        bio = (
            PROFILE_BIO_POOL[(account.id * 3 + index) % len(PROFILE_BIO_POOL)]
            + PROFILE_BIO_TAILS[(account.id * 5 + index) % len(PROFILE_BIO_TAILS)]
        )
        if any(word in display_name for word in forbidden):
            display_name = f"用户{account.id}"
            used_names.add(display_name)
        first_name = display_name
        last_name = ""
        username_base = (strategy.username_prefix_hint or _local_profile_username_base(account.id, index) or _romanize_name(display_name) or f"user{account.id}").lower()
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


def _unique_local_profile_display_name(name: str, used_names: set[str]) -> str:
    if name not in used_names:
        return name
    for tail in PROFILE_NAME_UNIQUE_TAILS:
        candidate = f"{name}{tail}"
        if candidate not in used_names:
            return candidate
    raise RuntimeError("本地随机中文名池不足，无法生成不重复昵称")


def _local_profile_display_name(account_id: int, index: int) -> str:
    seed = _local_profile_name_seed(account_id, index)
    variant = _local_profile_name_variant(account_id, index)
    if variant == 0:
        name = PROFILE_NAME_SHORTS[seed % len(PROFILE_NAME_SHORTS)]
    elif variant == 1:
        name = _local_compound_profile_name(seed)
    elif variant == 2:
        name = PROFILE_NAME_PLAYFUL[seed % len(PROFILE_NAME_PLAYFUL)]
    else:
        name = PROFILE_NAME_SCENES[seed % len(PROFILE_NAME_SCENES)]
    return name


def _local_profile_name_variant(account_id: int, index: int) -> int:
    return (
        account_id * PROFILE_NAME_VARIANT_ACCOUNT_FACTOR
        + index * PROFILE_NAME_VARIANT_INDEX_FACTOR
    ) % PROFILE_NAME_VARIANT_COUNT


def _local_profile_name_seed(account_id: int, index: int) -> int:
    return account_id * PROFILE_NAME_POOL_ACCOUNT_FACTOR + index * PROFILE_NAME_POOL_INDEX_FACTOR


def _local_compound_profile_name(seed: int) -> str:
    combo_count = len(PROFILE_NAME_PREFIXES) * len(PROFILE_NAME_SUFFIXES)
    combo_index = seed % combo_count
    prefix = PROFILE_NAME_PREFIXES[combo_index // len(PROFILE_NAME_SUFFIXES)]
    suffix = PROFILE_NAME_SUFFIXES[combo_index % len(PROFILE_NAME_SUFFIXES)]
    return f"{prefix}{suffix}"


def _local_profile_username_base(account_id: int, index: int) -> str:
    word = PROFILE_USERNAME_WORDS[(account_id + index) % len(PROFILE_USERNAME_WORDS)]
    trait = PROFILE_USERNAME_TRAITS[(account_id * 5 + index * 2) % len(PROFILE_USERNAME_TRAITS)]
    return f"{word}_{trait}"


def _is_batch_confirmed(confirm_text: str) -> bool:
    return confirm_text.strip() in {"确认", "确认创建", "确认创建批次", "确认执行", "确认加固"}


def _romanize_name(value: str) -> str:
    # Local deterministic fallback for preview tests; live AI output can provide richer candidates.
    return "tguser"


def _can_replace_display_name(display_name: str | None) -> bool:
    value = (display_name or "").strip()
    return value in GENERIC_DISPLAY_NAMES or bool(SYSTEM_DISPLAY_NAME_RE.match(value))


def _profile_update_values(account: TgAccount, item: TgAccountSecurityBatchItem, *, overwrite_existing: bool) -> ProfileUpdateValues:
    replace_display_name = overwrite_existing or _can_replace_display_name(account.display_name)
    replace_tg_name = replace_display_name or not account.tg_first_name
    replace_bio = overwrite_existing or not account.tg_bio
    display_name = item.generated_display_name if replace_display_name else account.display_name
    return ProfileUpdateValues(
        display_name=display_name or "",
        first_name=item.generated_first_name if replace_tg_name else account.tg_first_name,
        last_name=item.generated_last_name if replace_tg_name else account.tg_last_name,
        bio=item.generated_bio if replace_bio else account.tg_bio,
        replace_tg_name=replace_tg_name,
        replace_bio=replace_bio,
    )


def _avatar_source(session: Session, account: TgAccount, index: int, strategy) -> str:
    if strategy.avatar_sources:
        return strategy.avatar_sources[index % len(strategy.avatar_sources)]
    if strategy.mode in {"material_random", "random_from_material_pool", "sequential"}:
        material_sources = _material_avatar_sources(session, account.tenant_id, strategy)
        if material_sources:
            source_index = index if strategy.mode == "sequential" else account.id * 7 + index * 3
            return material_sources[source_index % len(material_sources)]
    if strategy.material_group_id:
        return f"material_group:{strategy.material_group_id}:{index + 1}"
    return ""


def _material_avatar_sources(session: Session, tenant_id: int, strategy) -> list[str]:
    stmt = (
        select(Material)
        .where(
            Material.tenant_id == tenant_id,
            Material.material_type == "图片",
            Material.review_status == "已审核",
            Material.source_kind == "upload",
            Material.mime_type.in_(["image/jpeg", "image/png", "image/webp"]),
            or_(Material.content != "", Material.cache_ready_status == "ready"),
        )
        .order_by(Material.id.asc())
    )
    materials = list(session.scalars(stmt))
    if strategy.material_group_id:
        group = session.get(MaterialGroup, strategy.material_group_id)
        if group and group.tenant_id == tenant_id and group.name:
            group_name = group.name.strip()
            materials = [material for material in materials if group_name in material.title or group_name in material.tags]
    preferred = [material for material in materials if "头像" in f"{material.title} {material.tags}".lower() or "avatar" in f"{material.title} {material.tags}".lower()]
    candidates = preferred or materials
    return [f"material:{material.id}" for material in candidates if _material_has_usable_avatar_source(material)]


def _material_has_usable_avatar_source(material: Material) -> bool:
    if material.content and Path(material.content).exists():
        return True
    return material.cache_ready_status == "ready" and bool(
        material.tg_cache_account_id and material.tg_cache_peer_id and material.tg_cache_message_id
    )


__all__ = [
    "account_security_batch_detail",
    "account_security_detail",
    "account_security_summary",
    "cancel_account_security_batch",
    "cleanup_devices_from_precheck",
    "create_account_security_batch",
    "create_device_cleanup_precheck",
    "drain_account_security_batches",
    "list_account_security_batches",
    "precheck_account_security_batch",
    "refresh_account_security",
    "reveal_managed_two_fa_password",
    "retry_account_security_batch",
    "rotate_managed_two_fa_password",
    "save_managed_two_fa_password",
]
