from __future__ import annotations

from collections import Counter
from datetime import timedelta
import hashlib
import hmac
import json
from uuid import uuid4

from sqlalchemy import select

from app.models import (
    TgAccount,
    TgAccountAuthorization,
    TgAccountAuthorizationSnapshot,
    TgAccountDeviceCleanupTarget,
    TgAccountSecurityBatch,
    TgAccountSecurityBatchItem,
)
from app.security import decrypt_secret, encrypt_secret, get_token_key
from app.timezone import as_beijing

from ._common import _now, audit, gateway
from .account_security.account_usage_guard import account_security_mutation_block
from .account_security.device_classification import (
    classify_account_authorization_snapshots,
    cleanup_eligible_authorization_snapshots,
)
from .developer_apps import credentials_for_authorization


CLEANUP_LOGIN_AGE = timedelta(hours=48)
TERMINAL_TARGET_STATUSES = frozenset({"succeeded", "already_absent", "failed"})


def create_device_cleanup_batch(
    session,
    tenant_id: int,
    account_ids: list[int],
    *,
    actor: str,
    reason: str,
    idempotency_key: str,
) -> TgAccountSecurityBatch:
    normalized = sorted(set(account_ids))
    if not normalized:
        raise ValueError("至少选择一个账号")
    if not idempotency_key.strip():
        raise ValueError("Idempotency-Key is required for device cleanup")
    existing = session.scalar(select(TgAccountSecurityBatch).where(
        TgAccountSecurityBatch.tenant_id == tenant_id,
        TgAccountSecurityBatch.idempotency_key == idempotency_key,
    ))
    if existing:
        _verify_idempotent_scope(session, existing, normalized)
        return existing
    batch = _new_batch(
        tenant_id,
        normalized,
        actor=actor,
        reason=reason,
        idempotency_key=idempotency_key,
    )
    session.add(batch)
    session.flush()
    reasons: Counter[str] = Counter()
    for account_id in normalized:
        item, reason_code = _build_cleanup_item(session, batch, account_id)
        session.add(item)
        if reason_code:
            reasons[reason_code] += 1
    _finish_batch_creation(session, batch, reasons, actor=actor)
    return batch


def _finish_batch_creation(session, batch, reasons: Counter[str], *, actor: str) -> None:
    batch.eligible_count = batch.requested_count - sum(reasons.values())
    batch.skipped_count = sum(reasons.values())
    batch.skipped_reason_counts = json.dumps(dict(reasons), sort_keys=True)
    batch.status = "running" if batch.eligible_count else "manual_required"
    batch.started_at = _now() if batch.eligible_count else None
    batch.finished_at = None if batch.eligible_count else _now()
    audit(
        session,
        tenant_id=batch.tenant_id,
        actor=actor,
        action="创建登录设备清理批次",
        target_type="account_security_batch",
        target_id=str(batch.id),
        detail=f"requested={batch.requested_count}; eligible={batch.eligible_count}; skipped={batch.skipped_count}",
    )
    session.commit()


def execute_device_cleanup_item(session, account: TgAccount, item: TgAccountSecurityBatchItem) -> list[str]:
    executor = _frozen_executor(session, account, item)
    if isinstance(executor, str):
        item.cleanup_status = "failed"
        item.failure_type = executor
        return [executor]
    try:
        _replace_remote_snapshots(session, account, executor)
    except Exception as exc:
        item.cleanup_status = "failed"
        item.failure_type = "device_list_read_failed"
        return [f"device_list_read_failed:{type(exc).__name__}"]
    protected = _protected_hashes(session, account.id)
    if not protected:
        item.cleanup_status = "failed"
        item.failure_type = "protected_authorization_hash_unproven"
        return ["protected_authorization_hash_unproven"]
    classified = classify_account_authorization_snapshots(session, account.id)
    if any(row["classification"] == "unknown" for row in classified):
        item.cleanup_status = "failed"
        item.failure_type = "device_cleanup_unresolved"
        return ["device_cleanup_unresolved"]
    targets = cleanup_eligible_authorization_snapshots(session, account)
    _freeze_targets(session, item, targets, protected=protected)
    failures = _execute_targets(session, account, executor, item=item)
    if item.status == "reconcile_unknown":
        return failures
    return failures + _final_readback(session, account, executor, item=item, protected=protected)


def _new_batch(tenant_id: int, account_ids: list[int], *, actor: str, reason: str, idempotency_key: str):
    return TgAccountSecurityBatch(
        tenant_id=tenant_id,
        action_types='["cleanup_devices"]',
        status="draft",
        total_count=len(account_ids),
        requested_count=len(account_ids),
        created_by=actor,
        confirmed_by=actor,
        confirm_text="确认",
        reason=reason,
        trace_id=uuid4().hex,
        idempotency_key=idempotency_key.strip(),
    )


def _build_cleanup_item(session, batch, account_id: int) -> tuple[TgAccountSecurityBatchItem, str]:
    account = session.get(TgAccount, account_id)
    reason = device_cleanup_eligibility_reason(session, batch.tenant_id, account)
    executor = _current_sv_authorization(session, account_id) if not reason else None
    item = TgAccountSecurityBatchItem(
        batch_id=batch.id,
        tenant_id=batch.tenant_id,
        account_id=account_id,
        status="skipped" if reason else "pending",
        precheck_status="skipped" if reason else "eligible",
        cleanup_status="skipped" if reason else "pending",
        skipped_reason=reason,
        failure_type=reason,
        executor_authorization_id=executor.id if executor else None,
        executor_fact_version=executor.fact_version if executor else 0,
        executor_telegram_login_at=executor.telegram_login_at if executor else None,
        trace_id=batch.trace_id,
        finished_at=_now() if reason else None,
    )
    return item, reason


def device_cleanup_eligibility_reason(session, tenant_id: int, account: TgAccount | None) -> str:
    if not account or account.tenant_id != tenant_id or account.deleted_at is not None:
        return "account_unavailable"
    usage_block = account_security_mutation_block(session, account, {"cleanup_devices"})
    if usage_block:
        return usage_block.failure_type or "account_action_forbidden"
    executor = _current_sv_authorization(session, account.id)
    if not executor or not executor.session_ciphertext:
        return "current_sv_authorization_unavailable"
    if not executor.telegram_login_at:
        return "login_time_missing"
    login_at = as_beijing(executor.telegram_login_at)
    if not login_at or _now() <= login_at + CLEANUP_LOGIN_AGE:
        return "login_age_not_over_48h"
    return ""


def _current_sv_authorization(session, account_id: int):
    rows = list(session.scalars(select(TgAccountAuthorization).where(
        TgAccountAuthorization.account_id == account_id,
        TgAccountAuthorization.is_current.is_(True),
        TgAccountAuthorization.provision_region_code == "sv",
        TgAccountAuthorization.disabled_at.is_(None),
    )))
    return rows[0] if len(rows) == 1 else None


def _frozen_executor(session, account, item):
    executor = session.get(TgAccountAuthorization, item.executor_authorization_id)
    if not executor or executor.account_id != account.id or not executor.is_current:
        return "current_authorization_changed"
    if executor.provision_region_code != "sv" or executor.fact_version != item.executor_fact_version:
        return "current_authorization_changed"
    if executor.telegram_login_at != item.executor_telegram_login_at:
        return "current_authorization_changed"
    return executor


def _replace_remote_snapshots(session, account, executor) -> None:
    credentials = credentials_for_authorization(session, executor)
    remote = gateway.list_authorizations(executor.session_ciphertext, credentials)
    session.query(TgAccountAuthorizationSnapshot).filter(
        TgAccountAuthorizationSnapshot.account_id == account.id
    ).delete()
    current = _now()
    for authorization in remote:
        session.add(TgAccountAuthorizationSnapshot(
            tenant_id=account.tenant_id,
            account_id=account.id,
            authorization_hash_ciphertext=encrypt_secret(authorization.authorization_hash),
            is_platform_trusted=bool(authorization.is_current),
            is_current_session=bool(authorization.is_current),
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
            scanned_at=current,
        ))
    session.flush()


def _protected_hashes(session, account_id: int) -> set[str]:
    rows = list(session.scalars(select(TgAccountAuthorization).where(
        TgAccountAuthorization.account_id == account_id,
        TgAccountAuthorization.protected_from_cleanup.is_(True),
        TgAccountAuthorization.remote_authorization_state != "revoked",
        TgAccountAuthorization.disabled_at.is_(None),
    )))
    values = {_raw_hash(row.telegram_authorization_hash_ciphertext) for row in rows}
    return set() if not rows or any(not value or value == "0" for value in values) else values


def _freeze_targets(session, item, snapshots, *, protected: set[str]) -> None:
    existing = list(session.scalars(select(TgAccountDeviceCleanupTarget).where(
        TgAccountDeviceCleanupTarget.batch_item_id == item.id,
    )))
    target_hashes = [_raw_hash(snapshot.authorization_hash_ciphertext) for snapshot in snapshots]
    if existing:
        if {row.target_hash_digest for row in existing} != {_digest(value) for value in target_hashes}:
            raise ValueError("device cleanup target set changed")
        return
    item.protected_manifest_digest = _set_digest(protected)
    item.target_set_digest = _set_digest(set(target_hashes))
    item.external_devices_before = len(target_hashes)
    for snapshot, raw_hash in zip(snapshots, target_hashes, strict=True):
        session.add(TgAccountDeviceCleanupTarget(
            batch_item_id=item.id,
            tenant_id=item.tenant_id,
            account_id=item.account_id,
            snapshot_id=snapshot.id,
            target_hash_ciphertext=snapshot.authorization_hash_ciphertext,
            target_hash_digest=_digest(raw_hash),
        ))
    session.commit()


def _execute_targets(session, account, executor, *, item) -> list[str]:
    targets = list(session.scalars(select(TgAccountDeviceCleanupTarget).where(
        TgAccountDeviceCleanupTarget.batch_item_id == item.id,
        TgAccountDeviceCleanupTarget.status.not_in(TERMINAL_TARGET_STATUSES),
    ).order_by(TgAccountDeviceCleanupTarget.id)))
    failures: list[str] = []
    credentials = credentials_for_authorization(session, executor)
    for target in targets:
        target.status = "remote_started"
        target.remote_effect_started_at = _now()
        item.remote_effect_started_at = item.remote_effect_started_at or _now()
        session.commit()
        try:
            result = gateway.cleanup_authorization(
                executor.session_ciphertext,
                _raw_hash(target.target_hash_ciphertext),
                credentials,
            )
        except Exception as exc:
            target.status = "remote_unknown"
            target.result_detail = type(exc).__name__
            item.status = "reconcile_unknown"
            item.cleanup_status = "reconcile_unknown"
            item.failure_type = "device_cleanup_remote_unknown"
            session.commit()
            return ["device_cleanup_remote_unknown"]
        _record_target_result(target, result)
        if not result.ok:
            failures.append(target.result_detail)
        session.commit()
    return failures


def _record_target_result(target, result) -> None:
    detail = str(result.detail or result.failure_type or "")
    if result.ok:
        target.status = "succeeded"
    elif _fresh_reset_forbidden(detail):
        target.status = "failed"
        detail = "telegram_fresh_reset_rejected"
    else:
        target.status = "failed"
    target.result_detail = detail
    target.finished_at = _now()


def _final_readback(session, account, executor, *, item, protected: set[str]) -> list[str]:
    try:
        _replace_remote_snapshots(session, account, executor)
    except Exception as exc:
        item.cleanup_status = "failed"
        item.failure_type = "device_list_read_failed"
        return [f"device_list_read_failed:{type(exc).__name__}"]
    remote_hashes = {
        _raw_hash(row.authorization_hash_ciphertext)
        for row in session.scalars(select(TgAccountAuthorizationSnapshot).where(
            TgAccountAuthorizationSnapshot.account_id == account.id,
        ))
    }
    targets = set(session.scalars(select(TgAccountDeviceCleanupTarget.target_hash_digest).where(
        TgAccountDeviceCleanupTarget.batch_item_id == item.id,
    )))
    item.final_readback_digest = _set_digest(remote_hashes)
    item.external_devices_after = sum(1 for value in remote_hashes if _digest(value) in targets)
    if not protected.issubset(remote_hashes):
        item.cleanup_status = "failed"
        item.failure_type = "protected_device_missing_after_cleanup"
        return ["protected_device_missing_after_cleanup"]
    classified = classify_account_authorization_snapshots(session, account.id)
    new_external = [row for row in classified if row["cleanup_eligible"] and _snapshot_digest(session, row["id"]) not in targets]
    if new_external:
        item.cleanup_status = "partial_failed"
        item.failure_type = "new_external_detected_after_apply"
        return ["new_external_detected_after_apply"]
    if item.external_devices_after:
        item.cleanup_status = "partial_failed"
        return ["device_cleanup_target_still_present"]
    item.cleanup_status = "succeeded"
    return []


def _verify_idempotent_scope(session, batch, account_ids: list[int]) -> None:
    stored = list(session.scalars(select(TgAccountSecurityBatchItem.account_id).where(
        TgAccountSecurityBatchItem.batch_id == batch.id,
    ).order_by(TgAccountSecurityBatchItem.account_id)))
    if stored != account_ids:
        raise ValueError("security_batch_target_changed")


def _snapshot_digest(session, snapshot_id: int) -> str:
    snapshot = session.get(TgAccountAuthorizationSnapshot, snapshot_id)
    return _digest(_raw_hash(snapshot.authorization_hash_ciphertext)) if snapshot else ""


def _raw_hash(ciphertext: str) -> str:
    return str(decrypt_secret(ciphertext) or ciphertext or "")


def _digest(value: str) -> str:
    return hmac.new(get_token_key(), f"device-authorization:{value}".encode(), hashlib.sha256).hexdigest()


def _set_digest(values: set[str]) -> str:
    return _digest(json.dumps(sorted(values), separators=(",", ":")))


def _fresh_reset_forbidden(detail: str) -> bool:
    normalized = detail.upper()
    return "FRESH_RESET_AUTHORISATION_FORBIDDEN" in normalized or "FRESH_RESET_AUTHORIZATION_FORBIDDEN" in normalized


def _mask_ip(value: str) -> str:
    parts = value.split(".") if value else []
    return f"{parts[0]}.{parts[1]}.*.*" if len(parts) == 4 else value


__all__ = [
    "create_device_cleanup_batch",
    "device_cleanup_eligibility_reason",
    "execute_device_cleanup_item",
]
