from __future__ import annotations

import hashlib

from sqlalchemy import func, select

from app.models import (
    AccountProxy,
    DeveloperAppSlotAssignment,
    TelegramDeveloperApp,
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrOperation,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationRestoreProbeFact,
    TgAuthorizationWakeBundle,
    TgAuthorizationWakeBundleCopy,
)

from .contracts import AuthorizationDrError
from .online_abc import (
    UNKNOWN_OPERATION_STATUSES,
    _audit_batch,
    _batch_by_key,
    _create_batch,
    _create_items,
    _fingerprint,
    _preview_body,
    _require_approval,
    _require_no_global_unknown,
    _require_no_open_batch,
    _require_runtime_off,
    online_abc_batch_status,
)


ACTIVE_OPERATION_STATUSES = {
    "pending", "waiting_login", "login_remote_started", "bundle_copies_verified",
    "ready_for_slot_commit", "slot_commit_prepared", "running", "approved",
} | UNKNOWN_OPERATION_STATUSES


def preview_full_online_abc_batch(
    session,
    tenant_id: int,
    *,
    idempotency_key: str,
    deployed_release_sha: str,
) -> dict:
    _require_runtime_off(session)
    _require_no_global_unknown(session)
    _require_accepted_canary(session, tenant_id)
    account_ids = list(session.scalars(select(TgAccount.id).where(
        TgAccount.tenant_id == tenant_id,
        TgAccount.deleted_at.is_(None),
        TgAccount.status == "在线",
    ).order_by(TgAccount.id)))
    if not account_ids:
        raise AuthorizationDrError("empty_target_set", "No online account is available")
    targets = [_freeze_full_target(session, tenant_id, account_id) for account_id in account_ids]
    body = _preview_body(tenant_id, idempotency_key, deployed_release_sha, targets)
    body["selection_mode"] = "all_online_accounts"
    body["classification_counts"] = _classification_counts(targets)
    return {**body, "fingerprint": _fingerprint(body)}


def apply_full_online_abc_batch(
    session,
    tenant_id: int,
    *,
    idempotency_key: str,
    deployed_release_sha: str,
    expected_fingerprint: str,
    requested_by: str,
    approved_by: str,
    approval_ref: str,
) -> dict:
    _require_approval(requested_by, approved_by, approval_ref)
    existing = _batch_by_key(session, tenant_id, idempotency_key)
    if existing:
        return online_abc_batch_status(session, existing.id)
    _require_no_open_batch(session, tenant_id)
    preview = preview_full_online_abc_batch(
        session,
        tenant_id,
        idempotency_key=idempotency_key,
        deployed_release_sha=deployed_release_sha,
    )
    if preview["fingerprint"] != expected_fingerprint:
        raise AuthorizationDrError("migration_fingerprint_conflict", "Full online ABC manifest changed")
    batch = _create_batch(session, preview, requested_by, approved_by, approval_ref)
    _create_items(session, batch, preview["targets"])
    _audit_batch(session, batch, "批准全量在线 ABC frozen-N manifest", approved_by)
    session.commit()
    return online_abc_batch_status(session, batch.id)


def _freeze_full_target(session, tenant_id: int, account_id: int) -> dict:
    account = session.get(TgAccount, account_id)
    primary = _structural_primary(session, account)
    active_operation = _active_operation(session, account_id)
    c = _classify_c(session, account_id)
    b = _classify_b(session, account, primary, c["app_id"])
    if primary is None or active_operation:
        b["plan"] = "blocked"
        c["plan"] = "blocked"
    return {
        "account_id": account_id,
        "primary_authorization_id": primary.id if primary else None,
        "primary_fact_version": primary.fact_version if primary else 0,
        "authorization_generation": account.authorization_generation,
        "authorization_fact_generation": account.authorization_fact_generation,
        "connection_generation": account.connection_generation,
        "primary_session_digest": _digest(primary.session_ciphertext or "") if primary else "",
        "app_b_id": b["app_id"],
        "app_b_credentials_version": b["credentials_version"],
        "app_b_assignment_purpose": b["assignment_purpose"],
        "app_b_assignment_version": b["assignment_version"],
        "proxy_id": b["proxy_id"],
        "source_c_authorization_id": c["authorization_id"],
        "source_c_fact_version": c["fact_version"],
        "source_c_slot_generation": c["slot_generation"],
        "standby_1_plan": b["plan"],
        "standby_2_plan": c["plan"],
    }


def _structural_primary(session, account):
    if not account or not account.current_authorization_id:
        return None
    primary = session.get(TgAccountAuthorization, account.current_authorization_id)
    valid = (
        primary and primary.is_current and primary.is_slot_current
        and primary.logical_slot in {"primary", "standby_1"}
        and primary.provision_region_code == "sv"
        and primary.session_ciphertext == account.session_ciphertext
        and primary.developer_app_id == account.developer_app_id
        and primary.session_ciphertext and primary.protected_from_cleanup
        and primary.disabled_at is None
    )
    return primary if valid else None


def _classify_b(session, account, primary, app_c_id: int | None) -> dict:
    current = session.scalar(select(TgAccountAuthorization).where(
        TgAccountAuthorization.account_id == account.id,
        TgAccountAuthorization.logical_slot == "standby_1",
        TgAccountAuthorization.is_slot_current.is_(True),
        TgAccountAuthorization.health_status == "healthy",
        TgAccountAuthorization.provision_region_code == "sv",
        TgAccountAuthorization.session_ciphertext.is_not(None),
        TgAccountAuthorization.disabled_at.is_(None),
    ).limit(1))
    if current and primary and current.id != primary.id and current.developer_app_id != primary.developer_app_id:
        route = _assignment_for_app(session, current.developer_app_id)
        return _b_result("already_qualified", current.developer_app, route, current.proxy_id or account.proxy_id)
    route, app = _backup_app(session, primary, app_c_id)
    proxy = session.get(AccountProxy, account.proxy_id) if account.proxy_id else None
    plan = "provision" if route and app and proxy and _proxy_ready(proxy) else "blocked"
    return _b_result(plan, app, route, proxy.id if proxy else None)


def _classify_c(session, account_id: int) -> dict:
    rows = list(session.scalars(select(TgAccountAuthorization).where(
        TgAccountAuthorization.account_id == account_id,
        TgAccountAuthorization.logical_slot == "standby_2",
        TgAccountAuthorization.is_slot_current.is_(True),
        TgAccountAuthorization.disabled_at.is_(None),
    )))
    assignment = session.get(DeveloperAppSlotAssignment, "standby_2_my")
    app_id = assignment.developer_app_id if assignment and assignment.status == "active" else None
    if len(rows) > 1:
        return _c_result("blocked", None, app_id)
    current = rows[0] if rows else None
    if current and _qualified_c(session, current):
        return _c_result("already_qualified", current, app_id)
    if current and _legacy_c(current, app_id):
        return _c_result("migrate", current, app_id)
    return _c_result("provision", current, app_id)


def _qualified_c(session, row) -> bool:
    if row.provision_region_code != "my" or row.health_status != "healthy" or not row.wake_bundle_id:
        return False
    bundle = session.get(TgAuthorizationWakeBundle, row.wake_bundle_id)
    if not bundle or not bundle.is_active or bundle.receipt_status != "active":
        return False
    copies = session.scalar(select(func.count()).select_from(TgAuthorizationWakeBundleCopy).where(
        TgAuthorizationWakeBundleCopy.bundle_id == bundle.id,
    ))
    probe = session.scalar(select(TgAuthorizationRestoreProbeFact).where(
        TgAuthorizationRestoreProbeFact.bundle_id == bundle.id,
    ).order_by(TgAuthorizationRestoreProbeFact.probe_generation.desc()).limit(1))
    return bool(
        bundle.kms_decrypt_status == "verified" and bundle.recoverable_copy_count == 2
        and bundle.protected_from_cleanup and int(copies or 0) == 2
        and bundle.auth_key_fingerprint_digest == row.auth_key_fingerprint_digest
        and bundle.telegram_user_id_digest == row.telegram_user_id_digest
        and probe and probe.status == "passed" and probe.session_parse_status == "passed"
        and probe.authorization_status == "authorized" and probe.identity_match_status == "matched"
        and probe.auth_key_match_status == "matched" and probe.source_client_disconnected
        and probe.probe_client_disconnected
    )


def _legacy_c(row, app_id: int | None) -> bool:
    return bool(
        app_id and row.developer_app_id == app_id and row.provision_region_code == "sv"
        and row.health_status == "healthy" and row.session_ciphertext
        and row.protected_from_cleanup
    )


def _backup_app(session, primary, app_c_id: int | None):
    if primary is None or app_c_id is None:
        return None, None
    excluded = {primary.developer_app_id, app_c_id}
    for purpose in ("standby_1_sv", "primary_sv"):
        assignment = session.get(DeveloperAppSlotAssignment, purpose)
        app = session.get(TelegramDeveloperApp, assignment.developer_app_id) if assignment else None
        if assignment and assignment.status == "active" and app and app.is_active and app.id not in excluded:
            return assignment, app
    return None, None


def _assignment_for_app(session, app_id: int):
    return session.scalar(select(DeveloperAppSlotAssignment).where(
        DeveloperAppSlotAssignment.developer_app_id == app_id,
        DeveloperAppSlotAssignment.status == "active",
    ).limit(1))


def _b_result(plan: str, app, assignment, proxy_id) -> dict:
    return {
        "plan": plan,
        "app_id": app.id if app else None,
        "credentials_version": app.credentials_version if app else 0,
        "assignment_purpose": assignment.slot_purpose if assignment else "",
        "assignment_version": assignment.assignment_version if assignment else 0,
        "proxy_id": proxy_id,
    }


def _c_result(plan: str, row, app_id: int | None) -> dict:
    return {
        "plan": plan,
        "app_id": app_id,
        "authorization_id": row.id if row else None,
        "fact_version": row.fact_version if row else 0,
        "slot_generation": row.slot_generation if row else 0,
    }


def _proxy_ready(proxy) -> bool:
    return proxy.status in {"healthy", "available", "normal", "active"}


def _active_operation(session, account_id: int):
    return session.scalar(select(TgAuthorizationDrOperation.id).where(
        TgAuthorizationDrOperation.account_id == account_id,
        TgAuthorizationDrOperation.status.in_(ACTIVE_OPERATION_STATUSES),
    ).limit(1))


def _require_accepted_canary(session, tenant_id: int) -> None:
    accepted = session.scalar(select(TgAuthorizationOnlineAbcBatch.id).where(
        TgAuthorizationOnlineAbcBatch.tenant_id == tenant_id,
        TgAuthorizationOnlineAbcBatch.selection_mode == "exact_ten_canary",
        TgAuthorizationOnlineAbcBatch.status == "accepted",
    ).limit(1))
    if not accepted:
        raise AuthorizationDrError(
            "online_abc_canary_observation_incomplete",
            "An accepted ten-account A/send canary is required",
        )


def _classification_counts(targets: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for target in targets:
        key = f"b:{target['standby_1_plan']}|c:{target['standby_2_plan']}"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


__all__ = ["apply_full_online_abc_batch", "preview_full_online_abc_batch"]
