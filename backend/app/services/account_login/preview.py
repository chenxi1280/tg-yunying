from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    AccountPool,
    DeveloperAppSlotAssignment,
    Material,
    Tenant,
    TgAccount,
    TgAccountLoginBatch,
    TgAccountPhoneFingerprintAlias,
)
from app.schemas.account_login import LoginBatchPrecheckItemOut, LoginBatchPrecheckOut
from app.security import get_token_key
from app.services._common import _now
from app.services.dedicated_account_pools import validate_account_pool_admission
from app.services.code_source_client import code_source_readiness
from app.services.account_post_login_init.policy import (
    PROFILE_POLICY_VERSION,
    require_post_login_init_ready,
)

from .contracts import BatchLoginError, ParsedLoginLine
from .identity import parse_login_lines, phone_fingerprints


PREVIEW_TTL_SECONDS = 300
ACTIVE_BATCH_STATUSES = ("queued", "running", "cancelling")


@dataclass(frozen=True)
class PreviewItem:
    line: ParsedLoginLine
    output: LoginBatchPrecheckItemOut


@dataclass(frozen=True)
class PreviewBuild:
    pool_id: int
    fingerprint: str
    state_digest: str
    queue_position: int
    items: tuple[PreviewItem, ...]


def require_batch_login_enabled(session: Session) -> None:
    capability = batch_login_capability(session)
    if capability["mode"] != "enabled" or not capability["readiness"]:
        raise BatchLoginError("account_batch_login_disabled", "批量登录当前不可用")


def batch_login_capability(session: Session) -> dict[str, object]:
    settings = get_settings()
    blockers = _readiness_blockers(session)
    return {
        "mode": settings.account_batch_login_mode,
        "max_lines": settings.account_batch_login_max_lines,
        "worker_concurrency": settings.account_batch_login_worker_concurrency,
        "item_deadline_seconds": settings.account_batch_login_item_deadline_seconds,
        "code_wait_seconds": settings.account_batch_login_code_wait_seconds,
        "poll_interval_seconds": settings.account_batch_login_poll_interval_seconds,
        "post_login_init_mode": settings.account_post_login_init_mode,
        "post_login_init_worker_concurrency": settings.account_post_login_init_worker_concurrency,
        "readiness": settings.account_batch_login_mode != "off" and not blockers,
        "blockers": blockers,
    }


def _readiness_blockers(session: Session) -> list[str]:
    settings = get_settings()
    blockers: list[str] = []
    if settings.account_batch_login_mode == "off":
        blockers.append("mode_off")
    if settings.tg_gateway_mode == "mock":
        blockers.append("telegram_gateway_mock")
    if settings.account_batch_login_host_concurrency < 1:
        blockers.append("host_rate_bucket_unconfigured")
    if settings.account_batch_login_host_min_interval_seconds <= 0:
        blockers.append("host_interval_unconfigured")
    if settings.account_batch_login_developer_app_concurrency < 1:
        blockers.append("developer_app_rate_bucket_unconfigured")
    if _missing_current_alias_count(session):
        blockers.append("phone_alias_backfill_required")
    return blockers


def _missing_current_alias_count(session: Session) -> int:
    settings = get_settings()
    alias_exists = select(TgAccountPhoneFingerprintAlias.id).where(
        TgAccountPhoneFingerprintAlias.tenant_id == TgAccount.tenant_id,
        TgAccountPhoneFingerprintAlias.account_id == TgAccount.id,
        TgAccountPhoneFingerprintAlias.key_version == settings.account_batch_phone_fingerprint_version,
        TgAccountPhoneFingerprintAlias.is_active.is_(True),
    ).exists()
    return int(session.scalar(select(func.count(TgAccount.id)).where(
        TgAccount.phone_ciphertext.is_not(None),
        TgAccount.deleted_at.is_(None),
        ~alias_exists,
    )) or 0)


def precheck_login_batch(session: Session, tenant_id: int, user_id: int, lines_text: str, pool_id: int) -> LoginBatchPrecheckOut:
    require_batch_login_enabled(session)
    build = build_preview(session, tenant_id, lines_text, pool_id)
    _require_code_source_readiness(build)
    settings = get_settings()
    now = _now()
    expires_at = now + timedelta(seconds=PREVIEW_TTL_SECONDS)
    credential_expires_at = now + timedelta(seconds=settings.account_batch_login_credential_ttl_seconds)
    return LoginBatchPrecheckOut(
        preview_token=_sign_preview_token(tenant_id, user_id, expires_at, build),
        preview_fingerprint=build.fingerprint,
        expires_at=expires_at,
        total_count=len(build.items),
        create_count=sum(item.output.route_hint == "create" for item in build.items),
        existing_probe_required_count=sum(item.output.route_hint == "existing_probe_required" for item in build.items),
        migrate_count=sum(bool(item.output.account_id and item.output.current_pool_id != pool_id) for item in build.items),
        queue_position=build.queue_position,
        estimated_seconds=(build.queue_position + len(build.items)) * settings.account_batch_login_item_deadline_seconds,
        worst_case_seconds=len(build.items) * settings.account_batch_login_item_deadline_seconds,
        credential_expires_at=credential_expires_at,
        items=[item.output for item in build.items],
    )


def build_preview(session: Session, tenant_id: int, lines_text: str, pool_id: int) -> PreviewBuild:
    settings = get_settings()
    pool = _require_pool(session, tenant_id, pool_id)
    require_post_login_init_ready(session, tenant_id, pool)
    lines = parse_login_lines(lines_text, max_lines=settings.account_batch_login_max_lines)
    items = tuple(_preview_item(session, tenant_id, line, pool_id) for line in lines)
    _require_unique_resolved_accounts(items)
    _require_account_policy_eligibility(session, pool, items)
    fingerprint = _preview_fingerprint(pool_id, lines)
    state_digest = _state_digest(session, pool, items)
    queue_position = int(session.scalar(select(func.count(TgAccountLoginBatch.id)).where(
        TgAccountLoginBatch.status.in_(ACTIVE_BATCH_STATUSES),
    )) or 0) + 1
    return PreviewBuild(pool_id, fingerprint, state_digest, queue_position, items)


def _require_unique_resolved_accounts(items: tuple[PreviewItem, ...]) -> None:
    account_ids = [item.output.account_id for item in items if item.output.account_id]
    if len(account_ids) != len(set(account_ids)):
        raise BatchLoginError(
            "duplicate_account_in_batch",
            "同一账号不能在同一批次重复出现",
        )


def _require_account_policy_eligibility(
    session: Session,
    pool: AccountPool,
    items: tuple[PreviewItem, ...],
) -> None:
    if pool.pool_purpose != "normal":
        return
    account_ids = [item.output.account_id for item in items if item.output.account_id]
    if not account_ids:
        return
    accounts = session.scalars(select(TgAccount).where(TgAccount.id.in_(account_ids)))
    if any(account.account_identity != "normal" for account in accounts):
        raise BatchLoginError(
            "account_identity_ineligible",
            "专用用途账号不能进入普通账号完整初始化批次",
        )


def _require_code_source_readiness(build: PreviewBuild) -> None:
    hosts = tuple(sorted({
        urlsplit(item.line.source.url).hostname or ""
        for item in build.items
    }))
    blocker = code_source_readiness(hosts)
    if blocker:
        raise BatchLoginError("account_batch_login_disabled", f"接码平台不可用：{blocker}")


def _require_pool(session: Session, tenant_id: int, pool_id: int) -> AccountPool:
    pool = session.get(AccountPool, pool_id)
    if not pool or pool.tenant_id != tenant_id:
        raise BatchLoginError("pool_admission_rejected", "目标分组不存在")
    try:
        validate_account_pool_admission(pool)
    except ValueError as exc:
        raise BatchLoginError("pool_admission_rejected", "目标分组不可用于批量登录") from exc
    return pool


def _preview_item(session: Session, tenant_id: int, line: ParsedLoginLine, pool_id: int) -> PreviewItem:
    account = _account_for_phone(session, tenant_id, line.phone)
    _assert_uuid_available(session, tenant_id, line.source.uuid_fingerprint, account)
    action, current_note, binding_version = _binding_preview(account, line.source.uuid_fingerprint)
    output = LoginBatchPrecheckItemOut(
        line_no=line.line_no,
        phone_masked=line.phone_masked,
        route_hint="existing_probe_required" if account else "create",
        account_id=account.id if account else None,
        current_pool_id=account.pool_id if account else None,
        code_source_note=f"{line.source.host} · {line.source.uuid_hint}",
        binding_action=action,
        current_binding_note=current_note,
        current_binding_version=binding_version,
    )
    return PreviewItem(line, output)


def _account_for_phone(session: Session, tenant_id: int, phone: str) -> TgAccount | None:
    versions = _accepted_versions()
    values = phone_fingerprints(tenant_id, phone, versions)
    aliases = list(session.scalars(select(TgAccountPhoneFingerprintAlias).where(
        TgAccountPhoneFingerprintAlias.tenant_id == tenant_id,
        TgAccountPhoneFingerprintAlias.is_active.is_(True),
        or_(*[
            and_(TgAccountPhoneFingerprintAlias.key_version == version, TgAccountPhoneFingerprintAlias.fingerprint == value)
            for version, value in values.items()
        ]),
    )))
    account_ids = {alias.account_id for alias in aliases}
    if len(account_ids) > 1:
        raise BatchLoginError("code_source_binding_conflict", "手机号身份别名冲突")
    if not account_ids:
        return None
    account = session.get(TgAccount, account_ids.pop())
    if not account:
        raise BatchLoginError("code_source_binding_conflict", "手机号关联到已删除账号")
    if account.deleted_at is not None:
        raise BatchLoginError("soft_deleted_account_conflict", "手机号关联到已删除账号")
    return account


def _assert_uuid_available(session: Session, tenant_id: int, fingerprint: str, account: TgAccount | None) -> None:
    bound = session.scalar(select(TgAccount).where(
        TgAccount.tenant_id == tenant_id,
        TgAccount.code_source_uuid_fingerprint == fingerprint,
    ))
    if bound and (not account or bound.id != account.id):
        raise BatchLoginError("code_source_binding_conflict", "UUID 已绑定其他账号")


def _binding_preview(account: TgAccount | None, fingerprint: str) -> tuple[str, str, int]:
    if not account or not account.code_source_uuid_fingerprint:
        return "bind", "", account.code_source_binding_version if account else 0
    current_note = account.code_source_note
    if account.code_source_uuid_fingerprint == fingerprint:
        return "keep", current_note, account.code_source_binding_version
    return "replace_required", current_note, account.code_source_binding_version


def _preview_fingerprint(pool_id: int, lines: list[ParsedLoginLine]) -> str:
    payload = [[line.line_no, line.phone, line.source.uuid_fingerprint] for line in lines]
    raw = json.dumps({"pool_id": pool_id, "lines": payload}, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def _state_digest(session: Session, pool: AccountPool, items: tuple[PreviewItem, ...]) -> str:
    tenant = session.get(Tenant, pool.tenant_id)
    state = {
        "pool": [pool.id, pool.is_enabled, pool.updated_at.isoformat() if pool.updated_at else ""],
        "post_init": [
            get_settings().account_post_login_init_mode,
            tenant.fixed_two_fa_password_version if tenant else 0,
            PROFILE_POLICY_VERSION,
            _profile_material_revision(session, pool.tenant_id),
            _abc_assignment_revision(session),
        ],
        "accounts": [
            [item.output.account_id, item.output.current_pool_id, item.output.current_binding_version]
            for item in items
        ],
    }
    return hashlib.sha256(json.dumps(state, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


def _profile_material_revision(session: Session, tenant_id: int) -> list[list[object]]:
    rows = session.execute(
        select(Material.id, Material.asset_version_id, Material.cache_ready_status).where(
            Material.tenant_id == tenant_id,
            Material.material_type == "图片",
            Material.review_status == "已审核",
        ).order_by(Material.id)
    )
    return [list(row) for row in rows]


def _abc_assignment_revision(session: Session) -> list[list[object]]:
    rows = session.execute(
        select(
            DeveloperAppSlotAssignment.slot_purpose,
            DeveloperAppSlotAssignment.developer_app_id,
            DeveloperAppSlotAssignment.assignment_version,
            DeveloperAppSlotAssignment.credentials_version,
            DeveloperAppSlotAssignment.status,
        ).order_by(DeveloperAppSlotAssignment.slot_purpose)
    )
    return [list(row) for row in rows]


def _accepted_versions() -> tuple[int, ...]:
    raw = get_settings().account_batch_phone_fingerprint_versions
    return tuple(sorted({int(value.strip()) for value in raw.split(",") if value.strip()}))


def _sign_preview_token(tenant_id: int, user_id: int, expires_at, build: PreviewBuild) -> str:
    payload = {"tenant": tenant_id, "user": user_id, "exp": int(expires_at.timestamp()), "fp": build.fingerprint, "state": build.state_digest}
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()).decode().rstrip("=")
    signature = hmac.new(get_token_key(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def verify_preview_token(token: str, tenant_id: int, user_id: int, build: PreviewBuild) -> None:
    try:
        encoded, signature = token.split(".", 1)
        expected = hmac.new(get_token_key(), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
    except (ValueError, json.JSONDecodeError) as exc:
        raise BatchLoginError("preview_stale", "预检凭据无效，请重新预检") from exc
    current = int(_now().timestamp())
    values = (payload.get("tenant"), payload.get("user"), payload.get("fp"), payload.get("state"))
    expected_values = (tenant_id, user_id, build.fingerprint, build.state_digest)
    if values != expected_values or int(payload.get("exp", 0)) < current:
        raise BatchLoginError("preview_stale", "预检结果已变化，请重新预检")


__all__ = [
    "PreviewBuild",
    "batch_login_capability",
    "build_preview",
    "precheck_login_batch",
    "require_batch_login_enabled",
    "verify_preview_token",
]
