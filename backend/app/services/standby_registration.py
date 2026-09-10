"""Register proved SV standby material without performing Telegram login."""
from dataclasses import dataclass

from sqlalchemy import func, select

from app.models import AccountStatus, TgAccountAuthorization
from app.security import encrypt_secret, encrypt_session
from app.timezone import as_beijing_aware
from ._common import _now, audit
from .account_authorization_constants import NEEDS_REPAIR_STATUS


@dataclass(frozen=True, kw_only=True)
class StandbyRegistration:
    flow: object
    app: object
    raw_session: str
    authorization_hash: str
    actor: str
    identity: object | None = None


def register_standby_authorization(session, account, registration):
    flow = registration.flow
    target_slot = standby_target_slot(session, account, flow)
    mark_same_role_for_repair(session, account, flow, target_slot=target_slot)
    asset = _new_asset(session, account, registration, target_slot=target_slot)
    session.add(asset)
    session.flush()
    flow.status = AccountStatus.ACTIVE.value
    flow.authorization_id = asset.id
    flow.temporary_session_ciphertext = None
    flow.phone_code_hash_ciphertext = None
    flow.code_preview = None
    audit(session, tenant_id=account.tenant_id, actor=registration.actor,
          action="完成备用授权登录", target_type="tg_account", target_id=str(account.id),
          detail=f"role={asset.role}; authorization_id={asset.id}; flow_id={flow.id}")
    return asset


def _new_asset(session, account, registration, *, target_slot):
    flow = registration.flow
    identity = registration.identity
    return TgAccountAuthorization(
        tenant_id=account.tenant_id, account_id=account.id, role=flow.authorization_role,
        logical_slot=target_slot, slot_generation=_next_slot_generation(session, account.id, target_slot),
        developer_app_id=flow.developer_app_id, developer_app_api_id_snapshot=registration.app.api_id,
        proxy_id=flow.proxy_id, session_ciphertext=encrypt_session(registration.raw_session),
        telegram_authorization_hash_ciphertext=encrypt_secret(registration.authorization_hash),
        status="standby", health_status="healthy", is_current=False,
        telegram_login_at=as_beijing_aware(_now()), last_success_at=_now(), created_by=registration.actor,
        telegram_user_id_digest=identity.telegram_user_id_digest if identity else "",
        auth_key_fingerprint_digest=identity.auth_key_fingerprint_digest if identity else "",
    )


def mark_same_role_for_repair(session, account, flow, *, target_slot=None):
    resolved_slot = target_slot or standby_target_slot(session, account, flow)
    rows = session.scalars(select(TgAccountAuthorization).where(
        TgAccountAuthorization.account_id == account.id,
        TgAccountAuthorization.disabled_at.is_(None),
    ))
    for row in rows:
        if row.role != flow.authorization_role and row.logical_slot != resolved_slot:
            continue
        if row.is_current:
            raise ValueError("当前业务授权占用备用登录目标槽")
        row.is_slot_current = False
        row.status = NEEDS_REPAIR_STATUS
        row.failure_reason = "同角色备用授权已重新登录，旧授权待确认后停用"


def standby_target_slot(session, account, flow):
    if flow.authorization_role != "standby_1":
        return flow.authorization_role
    current = session.get(TgAccountAuthorization, account.current_authorization_id) if account.current_authorization_id else None
    return "primary" if current and current.logical_slot == "standby_1" else "standby_1"


def _next_slot_generation(session, account_id, logical_slot):
    maximum = session.scalar(select(func.max(TgAccountAuthorization.slot_generation)).where(
        TgAccountAuthorization.account_id == account_id, TgAccountAuthorization.logical_slot == logical_slot,
    ))
    return int(maximum or 0) + 1
