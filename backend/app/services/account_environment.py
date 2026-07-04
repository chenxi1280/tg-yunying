from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AccountEnvironmentBinding,
    AccountProxy,
    AccountProxyBinding,
    FingerprintComboHistory,
    TelegramDeveloperApp,
    TgAccount,
    TgAccountAuthorization,
    TgAccountAuthorizationSnapshot,
)
from app.schemas.account_environment import AccountEnvironmentBindingOut, AccountEnvironmentBindingPatch
from app.services._common import _now, audit

EFFECT_BOUNDARY = "仅影响下一次连接 / 重登 / 新 session 初始化，不代表远端 Telegram 授权设备立即变更"


def list_account_environment_bindings(
    session: Session,
    *,
    tenant_id: int,
    search: str = "",
) -> list[AccountEnvironmentBindingOut]:
    authorizations = _authorization_rows(session, tenant_id)
    rows = [_project_authorization(session, authorization) for authorization in authorizations]
    needle = search.strip().lower()
    if not needle:
        return rows
    return [row for row in rows if _row_matches(row, needle)]


def patch_account_environment_binding(
    session: Session,
    *,
    tenant_id: int,
    account_id: int,
    payload: AccountEnvironmentBindingPatch,
    actor: str,
) -> AccountEnvironmentBindingOut:
    authorization = _require_authorization(session, tenant_id, account_id, payload)
    proxy = _require_proxy(session, tenant_id, payload.proxy_id)
    binding = _binding_for_authorization(session, tenant_id, account_id, payload)
    if binding is None:
        binding = _new_binding(tenant_id, account_id, payload, authorization)
        session.add(binding)
    _apply_binding(session, binding, payload, authorization, proxy)
    _record_combo(session, binding)
    session.flush()
    audit(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="保存账号授权环境",
        target_type="account_environment_binding",
        target_id=str(binding.id),
        detail=f"account_id={account_id}; developer_app_id={payload.developer_app_id}; authorization_id={payload.authorization_id}; effect=pending_next_session",
    )
    return _project_authorization(session, authorization)


def _authorization_rows(session: Session, tenant_id: int) -> list[TgAccountAuthorization]:
    stmt = (
        select(TgAccountAuthorization)
        .join(TgAccount, TgAccount.id == TgAccountAuthorization.account_id)
        .where(TgAccountAuthorization.tenant_id == tenant_id, TgAccount.deleted_at.is_(None))
        .order_by(TgAccount.id.asc(), TgAccountAuthorization.role.asc(), TgAccountAuthorization.id.asc())
    )
    return list(session.scalars(stmt).all())


def _project_authorization(session: Session, authorization: TgAccountAuthorization) -> AccountEnvironmentBindingOut:
    account = session.get(TgAccount, authorization.account_id)
    app = session.get(TelegramDeveloperApp, authorization.developer_app_id or 0) if authorization.developer_app_id else None
    binding = _active_binding(session, authorization)
    proxy = _binding_proxy(session, binding, authorization)
    snapshot = _observed_snapshot(session, authorization)
    return _environment_out(account, authorization, app, binding, proxy, snapshot)


def _active_binding(session: Session, authorization: TgAccountAuthorization) -> AccountEnvironmentBinding | None:
    stmt = select(AccountEnvironmentBinding).where(
        AccountEnvironmentBinding.tenant_id == authorization.tenant_id,
        AccountEnvironmentBinding.account_id == authorization.account_id,
        AccountEnvironmentBinding.developer_app_id == authorization.developer_app_id,
        AccountEnvironmentBinding.authorization_id == authorization.id,
        AccountEnvironmentBinding.session_role == authorization.role,
        AccountEnvironmentBinding.status == "active",
        AccountEnvironmentBinding.unbound_at.is_(None),
    )
    return session.scalar(stmt.limit(1))


def _binding_proxy(
    session: Session,
    binding: AccountEnvironmentBinding | None,
    authorization: TgAccountAuthorization,
) -> AccountProxy | None:
    proxy_id = binding.proxy_id if binding and binding.proxy_id else authorization.proxy_id
    return session.get(AccountProxy, int(proxy_id or 0)) if proxy_id else None


def _environment_out(
    account: TgAccount | None,
    authorization: TgAccountAuthorization,
    app: TelegramDeveloperApp | None,
    binding: AccountEnvironmentBinding | None,
    proxy: AccountProxy | None,
    snapshot: TgAccountAuthorizationSnapshot | None,
) -> AccountEnvironmentBindingOut:
    return AccountEnvironmentBindingOut(
        id=binding.id if binding else None,
        account_id=authorization.account_id,
        account_display_name=account.display_name if account else f"账号 #{authorization.account_id}",
        account_username=account.username or "" if account else "",
        phone_masked=account.phone_masked if account else "",
        account_status=account.status if account else "",
        developer_app_id=authorization.developer_app_id,
        developer_app_name=app.app_name if app else "",
        developer_app_api_id_snapshot=_api_id_snapshot(authorization, app),
        authorization_id=authorization.id,
        session_role=authorization.role,
        authorization_status=authorization.status,
        proxy_id=proxy.id if proxy else None,
        proxy_name=proxy.name if proxy else "",
        proxy_status=proxy.status if proxy else "",
        device_model=binding.device_model if binding else "",
        system_version=binding.system_version if binding else "",
        app_version=binding.app_version if binding else "",
        platform=binding.platform if binding else "",
        observed_device_model=snapshot.device_model if snapshot else "",
        observed_system_version=snapshot.system_version if snapshot else "",
        observed_app_version=snapshot.app_version if snapshot else "",
        observed_api_id=snapshot.api_id if snapshot else 0,
        lang_code=binding.lang_code if binding else "zh",
        system_lang_code=binding.system_lang_code if binding else "zh-CN",
        lang_pack=binding.lang_pack if binding else "",
        region_code=binding.region_code if binding else "CN",
        client_identity_key=binding.client_identity_key if binding else "",
        consistency_status=_consistency_status(binding, snapshot),
        effect_boundary=EFFECT_BOUNDARY,
        updated_at=binding.updated_at if binding else None,
    )


def _observed_snapshot(session: Session, authorization: TgAccountAuthorization) -> TgAccountAuthorizationSnapshot | None:
    api_id = int(authorization.developer_app_api_id_snapshot or 0)
    if api_id <= 0:
        return None
    stmt = (
        select(TgAccountAuthorizationSnapshot)
        .where(
            TgAccountAuthorizationSnapshot.tenant_id == authorization.tenant_id,
            TgAccountAuthorizationSnapshot.account_id == authorization.account_id,
            TgAccountAuthorizationSnapshot.api_id == api_id,
            TgAccountAuthorizationSnapshot.status == "active",
        )
        .order_by(TgAccountAuthorizationSnapshot.is_current_session.desc(), TgAccountAuthorizationSnapshot.scanned_at.desc(), TgAccountAuthorizationSnapshot.id.desc())
    )
    return session.scalar(stmt.limit(1))


def _require_authorization(
    session: Session,
    tenant_id: int,
    account_id: int,
    payload: AccountEnvironmentBindingPatch,
) -> TgAccountAuthorization:
    authorization = session.get(TgAccountAuthorization, payload.authorization_id)
    if authorization is None or authorization.tenant_id != tenant_id or authorization.account_id != account_id:
        raise ValueError("authorization_not_found")
    if authorization.role != payload.session_role or authorization.developer_app_id != payload.developer_app_id:
        raise ValueError("authorization_scope_mismatch")
    return authorization


def _require_proxy(session: Session, tenant_id: int, proxy_id: int | None) -> AccountProxy | None:
    if proxy_id is None:
        return None
    proxy = session.get(AccountProxy, proxy_id)
    if proxy is None or proxy.tenant_id != tenant_id:
        raise ValueError("proxy_not_found")
    return proxy


def _binding_for_authorization(
    session: Session,
    tenant_id: int,
    account_id: int,
    payload: AccountEnvironmentBindingPatch,
) -> AccountEnvironmentBinding | None:
    stmt = select(AccountEnvironmentBinding).where(
        AccountEnvironmentBinding.tenant_id == tenant_id,
        AccountEnvironmentBinding.account_id == account_id,
        AccountEnvironmentBinding.developer_app_id == payload.developer_app_id,
        AccountEnvironmentBinding.authorization_id == payload.authorization_id,
        AccountEnvironmentBinding.session_role == payload.session_role,
    )
    return session.scalar(stmt.limit(1))


def _new_binding(
    tenant_id: int,
    account_id: int,
    payload: AccountEnvironmentBindingPatch,
    authorization: TgAccountAuthorization,
) -> AccountEnvironmentBinding:
    return AccountEnvironmentBinding(
        tenant_id=tenant_id,
        account_id=account_id,
        developer_app_id=payload.developer_app_id,
        developer_app_api_id_snapshot=_api_id_snapshot(authorization, authorization.developer_app),
        authorization_id=payload.authorization_id,
        session_role=payload.session_role,
    )


def _apply_binding(
    session: Session,
    binding: AccountEnvironmentBinding,
    payload: AccountEnvironmentBindingPatch,
    authorization: TgAccountAuthorization,
    proxy: AccountProxy | None,
) -> None:
    binding.developer_app_api_id_snapshot = _api_id_snapshot(authorization, authorization.developer_app)
    binding.proxy_id = proxy.id if proxy else None
    binding.proxy_binding_id = _proxy_binding_id(session, binding, proxy)
    for field in _fingerprint_fields():
        setattr(binding, field, getattr(payload, field))
    binding.status = "active"
    binding.unbound_at = None
    binding.updated_at = _now()


def _proxy_binding_id(session: Session, binding: AccountEnvironmentBinding, proxy: AccountProxy | None) -> int | None:
    if proxy is None:
        return None
    existing = _active_proxy_binding(session, binding.account_id, proxy.id)
    if existing is not None:
        return existing.id
    proxy_binding = AccountProxyBinding(
        tenant_id=binding.tenant_id,
        account_id=binding.account_id,
        proxy_id=proxy.id,
        change_reason="account_environment_binding",
        bound_by="account_masks",
    )
    session.add(proxy_binding)
    session.flush()
    return proxy_binding.id


def _active_proxy_binding(session: Session, account_id: int, proxy_id: int) -> AccountProxyBinding | None:
    stmt = select(AccountProxyBinding).where(
        AccountProxyBinding.account_id == account_id,
        AccountProxyBinding.proxy_id == proxy_id,
        AccountProxyBinding.status == "active",
        AccountProxyBinding.unbound_at.is_(None),
    )
    return session.scalar(stmt.order_by(AccountProxyBinding.id.desc()).limit(1))


def _record_combo(session: Session, binding: AccountEnvironmentBinding) -> None:
    history = _combo_history(session, binding.tenant_id, binding.client_identity_key)
    if history is None:
        session.add(_new_combo_history(binding))
        return
    history.developer_app_id = binding.developer_app_id
    history.developer_app_api_id_snapshot = binding.developer_app_api_id_snapshot
    history.usage_count += 1
    history.last_bound_at = _now()


def _combo_history(session: Session, tenant_id: int, combo_key: str) -> FingerprintComboHistory | None:
    stmt = select(FingerprintComboHistory).where(
        FingerprintComboHistory.tenant_id == tenant_id,
        FingerprintComboHistory.combo_key == combo_key,
    )
    return session.scalar(stmt.limit(1))


def _new_combo_history(binding: AccountEnvironmentBinding) -> FingerprintComboHistory:
    return FingerprintComboHistory(
        tenant_id=binding.tenant_id,
        account_id=binding.account_id,
        developer_app_id=binding.developer_app_id,
        developer_app_api_id_snapshot=binding.developer_app_api_id_snapshot,
        authorization_id=binding.authorization_id,
        session_role=binding.session_role,
        combo_key=binding.client_identity_key,
        device_model=binding.device_model,
        system_version=binding.system_version,
        app_version=binding.app_version,
        platform=binding.platform,
        lang_code=binding.lang_code,
        system_lang_code=binding.system_lang_code,
        region_code=binding.region_code,
        usage_count=1,
    )


def _fingerprint_fields() -> tuple[str, ...]:
    return (
        "device_model",
        "system_version",
        "app_version",
        "platform",
        "lang_code",
        "system_lang_code",
        "lang_pack",
        "region_code",
        "client_identity_key",
    )


def _api_id_snapshot(authorization: TgAccountAuthorization, app: TelegramDeveloperApp | None) -> int:
    return int(authorization.developer_app_api_id_snapshot or (app.api_id if app else 0) or 0)


def _consistency_status(
    binding: AccountEnvironmentBinding | None,
    snapshot: TgAccountAuthorizationSnapshot | None,
) -> str:
    if binding is None:
        return "not_connected"
    if snapshot is None:
        return "pending_effect"
    expected = (binding.device_model, binding.system_version, binding.app_version)
    observed = (snapshot.device_model, snapshot.system_version, snapshot.app_version)
    return "observed_matched" if expected == observed else "observed_mismatch"


def _row_matches(row: AccountEnvironmentBindingOut, needle: str) -> bool:
    values = [
        row.account_display_name,
        row.account_username,
        row.phone_masked,
        row.account_status,
        row.developer_app_name,
        row.session_role,
        row.proxy_name,
        row.device_model,
        row.consistency_status,
    ]
    return any(needle in str(value or "").lower() for value in values)


__all__ = ["EFFECT_BOUNDARY", "list_account_environment_bindings", "patch_account_environment_binding"]
