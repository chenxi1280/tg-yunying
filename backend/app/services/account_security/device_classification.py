from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TgAccount, TgAccountAuthorization, TgAccountAuthorizationSnapshot


PLATFORM_APP = "platform_app"
NON_PLATFORM_APP = "non_platform_app"
OFFICIAL_ANCHOR_DEVICE = "official_anchor"
UNKNOWN_DEVICE = "unknown"
OFFICIAL_ANCHOR_API_IDS = {2040}
OFFICIAL_ANCHOR_APP_NAMES = {"telegram", "telegram desktop", "telegram ios", "telegram android", "telegram web"}


def classify_account_authorization_snapshots(session: Session, account_id: int) -> list[dict[str, Any]]:
    role_api_ids = _role_api_ids(session, account_id)
    account = session.get(TgAccount, account_id)
    _add_legacy_primary_api_id(account, role_api_ids)
    snapshots = _snapshots(session, account_id)
    return [_classified_snapshot(snapshot, role_api_ids) for snapshot in snapshots]


def cleanup_candidate_authorization_snapshots(
    session: Session,
    account: TgAccount,
) -> list[TgAccountAuthorizationSnapshot]:
    if account.account_identity == "code_receiver":
        return []
    role_api_ids = _role_api_ids(session, account.id)
    _add_legacy_primary_api_id(account, role_api_ids)
    return [
        snapshot
        for snapshot in _snapshots(session, account.id)
        if _can_cleanup_snapshot(snapshot, role_api_ids)
    ]


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


def _classified_snapshot(snapshot: TgAccountAuthorizationSnapshot, role_api_ids: dict[str, int]) -> dict[str, Any]:
    matched_roles = _matched_roles(snapshot.api_id, role_api_ids)
    classification = _classification(snapshot, matched_roles)
    return {
        "id": snapshot.id,
        "account_id": snapshot.account_id,
        "remote_api_id": snapshot.api_id,
        "app_name": snapshot.app_name,
        "device_model": snapshot.device_model,
        "platform": snapshot.platform,
        "classification": classification,
        "matched_roles": matched_roles,
        "cleanup_eligible": classification == NON_PLATFORM_APP,
        "scanned_at": snapshot.scanned_at,
    }


def _matched_roles(remote_api_id: int, role_api_ids: dict[str, int]) -> list[str]:
    if not remote_api_id:
        return []
    return [role for role, api_id in role_api_ids.items() if api_id == remote_api_id]


def _classification(snapshot: TgAccountAuthorizationSnapshot, matched_roles: list[str]) -> str:
    if not snapshot.api_id:
        return UNKNOWN_DEVICE
    if matched_roles:
        return PLATFORM_APP
    if _is_official_anchor(snapshot):
        return OFFICIAL_ANCHOR_DEVICE
    return NON_PLATFORM_APP


def _can_cleanup_snapshot(snapshot: TgAccountAuthorizationSnapshot, role_api_ids: dict[str, int]) -> bool:
    if snapshot.status != "active":
        return False
    if snapshot.is_current_session or snapshot.is_platform_trusted:
        return False
    return _classification(snapshot, _matched_roles(snapshot.api_id, role_api_ids)) == NON_PLATFORM_APP


def _is_official_anchor(snapshot: TgAccountAuthorizationSnapshot) -> bool:
    app_name = str(snapshot.app_name or "").strip().lower()
    return snapshot.api_id in OFFICIAL_ANCHOR_API_IDS or app_name in OFFICIAL_ANCHOR_APP_NAMES


def _authorization_api_id(row: TgAccountAuthorization) -> int:
    if row.developer_app_api_id_snapshot:
        return int(row.developer_app_api_id_snapshot)
    if row.developer_app:
        return int(row.developer_app.api_id)
    return 0
