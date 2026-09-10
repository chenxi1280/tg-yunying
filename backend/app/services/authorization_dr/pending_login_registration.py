"""Reconcile an already authorized login using a preserved independent peer."""
import hashlib
import json
from dataclasses import asdict, dataclass

from sqlalchemy import select

from app.integrations.telegram.authorization_fingerprint import authorization_fingerprint_digest
from app.models import TelegramDeveloperApp, TgAccount, TgAccountAuthorization, TgLoginFlow
from app.security import decrypt_secret, decrypt_session, encrypt_session
from app.services._common import audit, gateway
from app.services.developer_apps import credentials_for_authorization, credentials_for_developer_app
from app.services.standby_registration import StandbyRegistration, register_standby_authorization
from .contracts import AuthorizationDrError


@dataclass(frozen=True, kw_only=True)
class PendingLoginSelection:
    tenant_id: int
    account_id: int
    target_flow_id: int
    observer_flow_id: int


@dataclass(frozen=True, kw_only=True)
class RegistrationApproval:
    expected_fingerprint: str
    actor: str
    approval_ref: str


def preview_pending_login_registration(session, selection, *, telegram_gateway=gateway):
    account, target, observer = _inputs(session, selection)
    identity, peer_hash = _proof(session, account, (target, observer), telegram_gateway=telegram_gateway)
    payload = _payload(session, account, (target, observer), identity=identity, peer_hash=peer_hash)
    return {**payload, "fingerprint": _digest(payload)}


def apply_pending_login_registration(session, selection, approval, *, telegram_gateway=gateway):
    if not approval.actor.strip() or not approval.approval_ref.strip():
        raise AuthorizationDrError("reconcile_approval_required", "Registration approval is missing")
    _lock_inputs(session, selection)
    account, target, observer = _inputs(session, selection)
    identity, peer_hash = _proof(session, account, (target, observer), telegram_gateway=telegram_gateway)
    payload = _payload(session, account, (target, observer), identity=identity, peer_hash=peer_hash)
    if _digest(payload) != approval.expected_fingerprint:
        raise AuthorizationDrError("authorization_version_conflict", "Pending login proof changed")
    app = session.get(TelegramDeveloperApp, target.developer_app_id)
    asset = register_standby_authorization(session, account, StandbyRegistration(
        flow=target, app=app, raw_session=decrypt_secret(target.temporary_session_ciphertext),
        authorization_hash=peer_hash, actor=approval.actor, identity=identity,
    ))
    audit(session, tenant_id=account.tenant_id, actor=approval.actor,
          action="恢复已登录授权登记", target_type="tg_login_flow", target_id=str(target.id),
          detail=json.dumps({"selection": asdict(selection), "fingerprint": approval.expected_fingerprint,
                             "approval_ref": approval.approval_ref, "authorization_id": asset.id}, sort_keys=True))
    session.commit()
    session.refresh(asset)
    return asset


def _inputs(session, selection):
    account = session.get(TgAccount, selection.account_id)
    if not account or account.tenant_id != selection.tenant_id or account.deleted_at is not None:
        raise AuthorizationDrError("account_not_found", "Pending login account is unavailable")
    if selection.target_flow_id == selection.observer_flow_id:
        raise AuthorizationDrError("authorization_identity_mismatch", "Independent observer is required")
    target = _flow(session, account, selection.target_flow_id)
    observer = _flow(session, account, selection.observer_flow_id, observer=True)
    if target.authorization_role != "standby_1":
        raise AuthorizationDrError("authorization_slot_conflict", "Only SV standby login registration is supported")
    return account, target, observer


def _flow(session, account, flow_id, *, observer=False):
    flow = session.get(TgLoginFlow, flow_id)
    if not flow or flow.tenant_id != account.tenant_id or flow.account_id != account.id:
        raise AuthorizationDrError("login_flow_not_found", "Pending login flow does not belong to account")
    if observer and flow.authorization_id:
        _observer_asset(session, flow)
        return flow
    if flow.authorization_id or not flow.temporary_session_ciphertext or not flow.challenge_sent_at:
        raise AuthorizationDrError("login_flow_not_pending", "Original unregistered login material is unavailable")
    if flow.authorization_role not in {"standby_1", "primary"}:
        raise AuthorizationDrError("authorization_slot_conflict", "MY login flow is not an SV observer")
    return flow


def _proof(session, account, flows, *, telegram_gateway):
    target, observer = flows
    raw, credentials = _credentials(session, target)
    peer_raw, peer_credentials = _credentials(session, observer)
    identity = telegram_gateway.authorization_identity(raw, credentials)
    peer = telegram_gateway.authorization_identity(peer_raw, peer_credentials)
    if (not identity.telegram_user_id_digest or not identity.auth_key_fingerprint_digest
            or identity.telegram_user_id_digest != peer.telegram_user_id_digest
            or identity.auth_key_fingerprint_digest == peer.auth_key_fingerprint_digest):
        raise AuthorizationDrError("authorization_identity_mismatch", "Peer identity or AuthKey independence failed")
    current = session.get(TgAccountAuthorization, account.current_authorization_id) if account.current_authorization_id else None
    if current and current.telegram_user_id_digest and current.telegram_user_id_digest != identity.telegram_user_id_digest:
        raise AuthorizationDrError("authorization_identity_mismatch", "Login identity differs from account")
    views = telegram_gateway.list_authorizations(encrypt_session(peer_raw), peer_credentials)
    matches = [str(item.authorization_hash) for item in views if not item.is_current
               and authorization_fingerprint_digest(item) == identity.authorization_fingerprint_digest
               and str(item.authorization_hash or "") not in {"", "0"}]
    if len(matches) != 1:
        raise AuthorizationDrError("authorization_hash_unproven", "A unique nonzero peer hash is required")
    return identity, matches[0]


def _credentials(session, flow):
    if flow.authorization_id:
        asset = _observer_asset(session, flow)
        return decrypt_session(asset.session_ciphertext), credentials_for_authorization(session, asset)
    app = session.get(TelegramDeveloperApp, flow.developer_app_id)
    if not app:
        raise AuthorizationDrError("developer_app_unavailable", "Original login app is unavailable")
    return decrypt_secret(flow.temporary_session_ciphertext), credentials_for_developer_app(app)


def _payload(session, account, flows, *, identity, peer_hash):
    return {
        "account": [account.tenant_id, account.id, account.current_authorization_id,
                    account.authorization_generation, account.authorization_fact_generation, account.connection_generation],
        "flows": [_flow_version(flow) for flow in flows],
        "observer_asset": _observer_version(session, flows[1]),
        "apps": [[app.id, app.credentials_version, app.api_id] for app in
                 (session.get(TelegramDeveloperApp, flow.developer_app_id) for flow in flows)],
        "slots": [[row.id, row.fact_version, row.slot_generation, row.is_current, row.is_slot_current, row.status]
                  for row in session.scalars(select(TgAccountAuthorization).where(
                      TgAccountAuthorization.account_id == account.id,
                      TgAccountAuthorization.disabled_at.is_(None)).order_by(TgAccountAuthorization.id))],
        "telegram_user_id_digest": identity.telegram_user_id_digest,
        "auth_key_fingerprint_digest": identity.auth_key_fingerprint_digest,
        "authorization_fingerprint_digest": identity.authorization_fingerprint_digest,
        "peer_hash_digest": _digest(peer_hash),
    }


def _observer_asset(session, flow):
    asset = session.get(TgAccountAuthorization, flow.authorization_id)
    if (not asset or asset.tenant_id != flow.tenant_id or asset.account_id != flow.account_id
            or asset.developer_app_id != flow.developer_app_id
            or asset.disabled_at is not None or not asset.is_slot_current
            or asset.provision_region_code != "sv" or asset.health_status != "healthy"
            or asset.status not in {"active", "standby"} or not asset.session_ciphertext):
        raise AuthorizationDrError("authorization_observer_unavailable", "Registered observer is not a healthy SV asset")
    return asset


def _observer_version(session, flow):
    if not flow.authorization_id:
        return None
    asset = _observer_asset(session, flow)
    return [asset.id, asset.fact_version, asset.slot_generation, asset.developer_app_id,
            asset.telegram_user_id_digest, asset.auth_key_fingerprint_digest, _digest(asset.session_ciphertext)]


def _flow_version(flow):
    return [flow.id, flow.flow_version, flow.status, flow.authorization_id, flow.developer_app_id,
            flow.proxy_id, flow.authorization_role, _digest(flow.temporary_session_ciphertext)]


def _lock_inputs(session, selection):
    session.scalar(select(TgAccount).where(TgAccount.id == selection.account_id).with_for_update()
                   .execution_options(populate_existing=True))
    flows = list(session.scalars(select(TgLoginFlow).where(TgLoginFlow.id.in_(
        [selection.target_flow_id, selection.observer_flow_id])).order_by(TgLoginFlow.id)
        .with_for_update().execution_options(populate_existing=True)))
    asset_ids = [flow.authorization_id for flow in flows if flow.authorization_id]
    if asset_ids:
        list(session.scalars(select(TgAccountAuthorization).where(TgAccountAuthorization.id.in_(asset_ids))
            .order_by(TgAccountAuthorization.id).with_for_update().execution_options(populate_existing=True)))


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
