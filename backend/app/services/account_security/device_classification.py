from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountPool, TgAccount, TgAccountAuthorization, TgAccountAuthorizationSnapshot
from app.security import decrypt_secret
from app.services.account_usage_policy import assert_account_action_allowed


PLATFORM_APP = "platform_app"
NON_PLATFORM_APP = "non_platform_app"
OFFICIAL_ANCHOR_DEVICE = "official_anchor"
UNKNOWN_DEVICE = "unknown"
OFFICIAL_ANCHOR_API_IDS = {2040}
OFFICIAL_ANCHOR_APP_NAMES = {"telegram", "telegram desktop", "telegram ios", "telegram android", "telegram web"}
OFFICIAL_ANCHOR_KEEP_COUNT = 1


def classify_account_authorization_snapshots(session: Session, account_id: int) -> list[dict[str, Any]]:
    role_api_ids = _role_api_ids(session, account_id)
    account = session.get(TgAccount, account_id)
    _add_legacy_primary_api_id(account, role_api_ids)
    protected_hashes = _protected_authorization_hashes(session, account)
    snapshots = _snapshots(session, account_id)
    official_anchor_ids = _official_anchor_snapshot_ids(snapshots)
    return [_classified_snapshot(snapshot, role_api_ids, protected_hashes, official_anchor_ids) for snapshot in snapshots]


def cleanup_candidate_authorization_snapshots(
    session: Session,
    account: TgAccount,
) -> list[TgAccountAuthorizationSnapshot]:
    if not _device_cleanup_allowed(session, account):
        return []
    role_api_ids = _role_api_ids(session, account.id)
    _add_legacy_primary_api_id(account, role_api_ids)
    protected_hashes = _protected_authorization_hashes(session, account)
    snapshots = _snapshots(session, account.id)
    official_anchor_ids = _official_anchor_snapshot_ids(snapshots)
    return [
        snapshot
        for snapshot in snapshots
        if _can_cleanup_snapshot(snapshot, protected_hashes, official_anchor_ids)
    ]


def _device_cleanup_allowed(session: Session, account: TgAccount) -> bool:
    pool = session.get(AccountPool, account.pool_id) if account.pool_id is not None else None
    try:
        assert_account_action_allowed(account, pool, "device_cleanup")
        return True
    except ValueError:
        return False


def _role_api_ids(session: Session, account_id: int) -> dict[str, int]:
    rows = session.scalars(
        select(TgAccountAuthorization).where(
            TgAccountAuthorization.account_id == account_id,
            TgAccountAuthorization.disabled_at.is_(None),
        )
    )
    result: dict[str, int] = {}
    for row in rows:
        api_id = _authorization_api_id(row)
        if api_id:
            result[row.role] = api_id
    return result


def _add_legacy_primary_api_id(account: TgAccount | None, role_api_ids: dict[str, int]) -> None:
    if not account or "primary" in role_api_ids:
        return
    if account.developer_api_id:
        role_api_ids["primary"] = int(account.developer_api_id)


def _snapshots(session: Session, account_id: int) -> list[TgAccountAuthorizationSnapshot]:
    return list(
        session.scalars(
            select(TgAccountAuthorizationSnapshot)
            .where(TgAccountAuthorizationSnapshot.account_id == account_id)
            .order_by(TgAccountAuthorizationSnapshot.id.asc())
        )
    )


def _classified_snapshot(
    snapshot: TgAccountAuthorizationSnapshot,
    role_api_ids: dict[str, int],
    protected_hashes: set[str],
    official_anchor_ids: set[int],
) -> dict[str, Any]:
    matched_roles = _matched_roles(snapshot.api_id, role_api_ids)
    classification = _classification(snapshot, matched_roles, protected_hashes, official_anchor_ids)
    return {
        "id": snapshot.id,
        "account_id": snapshot.account_id,
        "remote_api_id": snapshot.api_id,
        "app_name": snapshot.app_name,
        "device_model": snapshot.device_model,
        "platform": snapshot.platform,
        "classification": classification,
        "matched_roles": matched_roles,
        "cleanup_eligible": _can_cleanup_snapshot(snapshot, protected_hashes, official_anchor_ids),
        "scanned_at": snapshot.scanned_at,
    }


def _matched_roles(remote_api_id: int, role_api_ids: dict[str, int]) -> list[str]:
    if not remote_api_id:
        return []
    return [role for role, api_id in role_api_ids.items() if api_id == remote_api_id]


def _classification(
    snapshot: TgAccountAuthorizationSnapshot,
    matched_roles: list[str],
    protected_hashes: set[str],
    official_anchor_ids: set[int],
) -> str:
    if not snapshot.api_id:
        return UNKNOWN_DEVICE
    if snapshot.is_current_session or matched_roles or _has_protected_hash(snapshot, protected_hashes):
        return PLATFORM_APP
    if snapshot.id in official_anchor_ids:
        return OFFICIAL_ANCHOR_DEVICE
    return NON_PLATFORM_APP


def _can_cleanup_snapshot(
    snapshot: TgAccountAuthorizationSnapshot,
    protected_hashes: set[str],
    official_anchor_ids: set[int],
) -> bool:
    if snapshot.status != "active":
        return False
    if snapshot.is_current_session or _has_protected_hash(snapshot, protected_hashes):
        return False
    if snapshot.id in official_anchor_ids:
        return False
    return bool(snapshot.api_id)


def _official_anchor_snapshot_ids(snapshots: list[TgAccountAuthorizationSnapshot]) -> set[int]:
    anchors = [snapshot for snapshot in snapshots if _is_official_anchor(snapshot)]
    sorted_anchors = sorted(anchors, key=_official_anchor_sort_key, reverse=True)
    return {snapshot.id for snapshot in sorted_anchors[:OFFICIAL_ANCHOR_KEEP_COUNT]}


def _official_anchor_sort_key(snapshot: TgAccountAuthorizationSnapshot) -> tuple[object, object, int]:
    return (snapshot.date_active or snapshot.date_created or snapshot.scanned_at, snapshot.api_id == 2040, snapshot.id)


def _is_official_anchor(snapshot: TgAccountAuthorizationSnapshot) -> bool:
    app_name = str(snapshot.app_name or "").strip().lower()
    return snapshot.api_id in OFFICIAL_ANCHOR_API_IDS or app_name in OFFICIAL_ANCHOR_APP_NAMES


def _authorization_api_id(row: TgAccountAuthorization) -> int:
    if row.developer_app_api_id_snapshot:
        return int(row.developer_app_api_id_snapshot)
    if row.developer_app:
        return int(row.developer_app.api_id)
    return 0


def _protected_authorization_hashes(session: Session, account: TgAccount | None) -> set[str]:
    if account is None:
        return set()
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
        if _usable_hash(value)
    }


def _has_protected_hash(snapshot: TgAccountAuthorizationSnapshot, protected_hashes: set[str]) -> bool:
    raw_hash = decrypt_secret(snapshot.authorization_hash_ciphertext) or snapshot.authorization_hash_ciphertext
    return bool(_usable_hash(raw_hash) and raw_hash in protected_hashes)


def _usable_hash(value: str | None) -> bool:
    raw = str(value or "").strip()
    return raw not in {"", "0"}
