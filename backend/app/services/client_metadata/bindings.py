from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import (
    AccountEnvironmentBinding,
    AccountProxy,
    AccountProxyBinding,
    FingerprintComboHistory,
    TgAccount,
    TgAccountAuthorization,
)
from app.services._common import _now

ACTIVE_AUTHORIZATION_STATUSES = {"active", "standby"}
MAX_COMBO_ATTEMPTS = 12
IOS_WEIGHT_BUCKETS = 8
WEIGHT_BUCKETS = 10

IOS_DEVICE_MODELS = ("iPhone 13", "iPhone 14", "iPhone 15", "iPhone 15 Pro")
IOS_SYSTEM_VERSIONS = ("iOS 16.7", "iOS 17.4", "iOS 17.5")
IOS_APP_VERSIONS = ("10.12.0", "10.13.1", "10.14.1")
ANDROID_DEVICE_MODELS = ("Pixel 7", "Samsung SM-S9180", "Xiaomi 14", "OPPO PGEM10")
ANDROID_SYSTEM_VERSIONS = ("Android 13", "Android 14")
ANDROID_APP_VERSIONS = ("10.12.1", "10.13.2", "10.14.3")


@dataclass(frozen=True)
class ClientMetadata:
    device_model: str
    system_version: str
    app_version: str
    platform: str
    lang_code: str
    system_lang_code: str
    lang_pack: str
    region_code: str
    client_identity_key: str

    def to_payload(self) -> dict[str, str]:
        return {
            "device_model": self.device_model,
            "system_version": self.system_version,
            "app_version": self.app_version,
            "platform": self.platform,
            "lang_code": self.lang_code,
            "system_lang_code": self.system_lang_code,
            "lang_pack": self.lang_pack,
            "region_code": self.region_code,
            "client_identity_key": self.client_identity_key,
        }


@dataclass(frozen=True)
class SearchJoinEnvironment:
    authorization_id: int
    session_role: str
    developer_app_id: int
    developer_app_api_id: int
    proxy_id: int
    proxy_name: str
    proxy_binding_id: int
    binding_id: str
    client_metadata: dict[str, str]


@dataclass(frozen=True)
class GenerationSeed:
    account_id: int
    developer_app_id: int
    authorization_id: int
    session_role: str
    salt: int


@dataclass(frozen=True)
class BindingInput:
    account: TgAccount
    authorization: TgAccountAuthorization
    proxy: AccountProxy
    proxy_binding_id: int
    metadata: ClientMetadata


@dataclass(frozen=True)
class EnvironmentTarget:
    account: TgAccount
    authorization: TgAccountAuthorization
    proxy: AccountProxy


def ensure_search_join_environment(session: Session, account: TgAccount) -> SearchJoinEnvironment | None:
    authorization = _active_authorization(session, account.id)
    if authorization is None:
        return None
    binding = _existing_binding(session, account.id, authorization)
    if binding is None:
        proxy = _healthy_proxy(session, authorization.proxy_id)
        if proxy is None:
            return None
        binding = _create_binding(session, EnvironmentTarget(account, authorization, proxy))
    else:
        _hydrate_binding_app_scope(binding, authorization)
        proxy = _healthy_proxy(session, binding.proxy_id)
        if proxy is None:
            return None
    return _environment_from_binding(binding, proxy)


def _active_authorization(session: Session, account_id: int) -> TgAccountAuthorization | None:
    stmt = (
        select(TgAccountAuthorization)
        .where(
            TgAccountAuthorization.account_id == account_id,
            TgAccountAuthorization.disabled_at.is_(None),
            TgAccountAuthorization.status.in_(ACTIVE_AUTHORIZATION_STATUSES),
            TgAccountAuthorization.session_ciphertext.is_not(None),
            TgAccountAuthorization.session_ciphertext != "",
        )
        .order_by(TgAccountAuthorization.is_current.desc(), TgAccountAuthorization.role.asc(), TgAccountAuthorization.id.asc())
    )
    return session.scalar(stmt.limit(1))


def _healthy_proxy(session: Session, proxy_id: int | None) -> AccountProxy | None:
    proxy = session.get(AccountProxy, int(proxy_id or 0)) if proxy_id else None
    if proxy and proxy.status == "healthy" and proxy.alert_status == "normal":
        return proxy
    return None


def _existing_binding(
    session: Session,
    account_id: int,
    authorization: TgAccountAuthorization,
) -> AccountEnvironmentBinding | None:
    stmt = select(AccountEnvironmentBinding).where(
        AccountEnvironmentBinding.account_id == account_id,
        or_(
            AccountEnvironmentBinding.developer_app_id == authorization.developer_app_id,
            AccountEnvironmentBinding.developer_app_id.is_(None),
        ),
        AccountEnvironmentBinding.authorization_id == authorization.id,
        AccountEnvironmentBinding.session_role == authorization.role,
        AccountEnvironmentBinding.status == "active",
        AccountEnvironmentBinding.unbound_at.is_(None),
    ).order_by(AccountEnvironmentBinding.developer_app_id.is_(None).asc(), AccountEnvironmentBinding.id.asc())
    return session.scalar(stmt.limit(1))


def _create_binding(session: Session, target: EnvironmentTarget) -> AccountEnvironmentBinding:
    metadata = _available_metadata(
        session,
        target.account.id,
        int(target.authorization.developer_app_id or 0),
        target.authorization.id,
        target.authorization.role,
    )
    proxy_binding = _ensure_proxy_binding(session, target)
    binding_input = BindingInput(target.account, target.authorization, target.proxy, proxy_binding.id, metadata)
    binding = _new_binding(binding_input)
    session.add(binding)
    _record_combo(session, binding)
    session.flush()
    return binding


def _available_metadata(
    session: Session,
    account_id: int,
    developer_app_id: int,
    authorization_id: int,
    role: str,
) -> ClientMetadata:
    for salt in range(MAX_COMBO_ATTEMPTS):
        metadata = _generate_metadata(GenerationSeed(account_id, developer_app_id, authorization_id, role, salt))
        if not _combo_used_by_account_app(session, account_id, developer_app_id, metadata.client_identity_key):
            return metadata
    raise ValueError("client_metadata_combo_reused")


def _generate_metadata(seed: GenerationSeed) -> ClientMetadata:
    digest = _digest(f"{seed.account_id}:{seed.developer_app_id}:{seed.authorization_id}:{seed.session_role}:{seed.salt}")
    if int(digest[0:2], 16) % WEIGHT_BUCKETS < IOS_WEIGHT_BUCKETS:
        return _ios_metadata(digest)
    return _android_metadata(digest)


def _ios_metadata(digest: str) -> ClientMetadata:
    device_model = _pick(IOS_DEVICE_MODELS, digest, 2)
    system_version = _pick(IOS_SYSTEM_VERSIONS, digest, 4)
    app_version = _pick(IOS_APP_VERSIONS, digest, 6)
    return _metadata("ios", device_model, system_version, app_version, digest)


def _android_metadata(digest: str) -> ClientMetadata:
    device_model = _pick(ANDROID_DEVICE_MODELS, digest, 2)
    system_version = _pick(ANDROID_SYSTEM_VERSIONS, digest, 4)
    app_version = _pick(ANDROID_APP_VERSIONS, digest, 6)
    return _metadata("android", device_model, system_version, app_version, digest)


def _metadata(platform: str, device_model: str, system_version: str, app_version: str, digest: str) -> ClientMetadata:
    identity = _digest(f"{platform}:{device_model}:{system_version}:{app_version}:{digest[8:20]}")[:40]
    return ClientMetadata(device_model, system_version, app_version, platform, "zh", "zh-CN", "", "CN", identity)


def _combo_used_by_account_app(session: Session, account_id: int, developer_app_id: int, identity_key: str) -> bool:
    stmt = select(AccountEnvironmentBinding.id).where(
        AccountEnvironmentBinding.account_id == account_id,
        AccountEnvironmentBinding.developer_app_id == developer_app_id,
        AccountEnvironmentBinding.client_identity_key == identity_key,
        AccountEnvironmentBinding.status == "active",
        AccountEnvironmentBinding.unbound_at.is_(None),
    )
    return session.scalar(stmt.limit(1)) is not None


def _ensure_proxy_binding(session: Session, target: EnvironmentTarget) -> AccountProxyBinding:
    existing = _active_proxy_binding(session, target, target.proxy.id)
    if existing is not None:
        return existing
    binding = AccountProxyBinding(
        tenant_id=target.account.tenant_id,
        account_id=target.account.id,
        developer_app_id=target.authorization.developer_app_id,
        developer_app_api_id_snapshot=int(target.authorization.developer_app_api_id_snapshot or 0),
        authorization_id=target.authorization.id,
        session_role=target.authorization.role,
        proxy_id=target.proxy.id,
        change_reason="search_join_environment_binding",
        bound_by="system",
    )
    session.add(binding)
    session.flush()
    return binding


def _hydrate_binding_app_scope(
    binding: AccountEnvironmentBinding,
    authorization: TgAccountAuthorization,
) -> None:
    if binding.developer_app_id:
        return
    binding.developer_app_id = authorization.developer_app_id
    binding.developer_app_api_id_snapshot = int(authorization.developer_app_api_id_snapshot or 0)
    binding.updated_at = _now()


def _active_proxy_binding(
    session: Session,
    target: EnvironmentTarget,
    proxy_id: int,
) -> AccountProxyBinding | None:
    stmt = select(AccountProxyBinding).where(
        AccountProxyBinding.tenant_id == target.account.tenant_id,
        AccountProxyBinding.account_id == target.account.id,
        AccountProxyBinding.developer_app_id == target.authorization.developer_app_id,
        AccountProxyBinding.authorization_id == target.authorization.id,
        AccountProxyBinding.session_role == target.authorization.role,
        AccountProxyBinding.proxy_id == proxy_id,
        AccountProxyBinding.status == "active",
        AccountProxyBinding.unbound_at.is_(None),
    )
    return session.scalar(stmt.order_by(AccountProxyBinding.id.desc()).limit(1))


def _new_binding(binding_input: BindingInput) -> AccountEnvironmentBinding:
    payload = binding_input.metadata.to_payload()
    return AccountEnvironmentBinding(
        tenant_id=binding_input.account.tenant_id,
        account_id=binding_input.account.id,
        developer_app_id=binding_input.authorization.developer_app_id,
        developer_app_api_id_snapshot=int(binding_input.authorization.developer_app_api_id_snapshot or 0),
        authorization_id=binding_input.authorization.id,
        session_role=binding_input.authorization.role,
        proxy_binding_id=binding_input.proxy_binding_id,
        proxy_id=binding_input.proxy.id,
        **payload,
    )


def _record_combo(session: Session, binding: AccountEnvironmentBinding) -> None:
    history = _combo_history(session, binding.tenant_id, binding.client_identity_key)
    if history is None:
        session.add(_new_combo_history(binding))
        return
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


def _environment_from_binding(binding: AccountEnvironmentBinding, proxy: AccountProxy) -> SearchJoinEnvironment:
    if not _binding_complete(binding):
        raise ValueError("client_metadata_incomplete")
    proxy_binding_id = int(binding.proxy_binding_id or 0)
    if proxy_binding_id <= 0:
        raise ValueError("proxy_binding_missing")
    return SearchJoinEnvironment(
        authorization_id=binding.authorization_id,
        session_role=binding.session_role,
        developer_app_id=int(binding.developer_app_id or 0),
        developer_app_api_id=int(binding.developer_app_api_id_snapshot or 0),
        proxy_id=int(binding.proxy_id or proxy.id),
        proxy_name=proxy.name,
        proxy_binding_id=proxy_binding_id,
        binding_id=binding.id,
        client_metadata=_metadata_from_binding(binding),
    )


def _binding_complete(binding: AccountEnvironmentBinding) -> bool:
    values = [binding.device_model, binding.system_version, binding.app_version, binding.platform, binding.client_identity_key]
    return all(str(value or "").strip() for value in values)


def _metadata_from_binding(binding: AccountEnvironmentBinding) -> dict[str, str]:
    return {
        "device_model": binding.device_model,
        "system_version": binding.system_version,
        "app_version": binding.app_version,
        "platform": binding.platform,
        "lang_code": binding.lang_code,
        "system_lang_code": binding.system_lang_code,
        "lang_pack": binding.lang_pack,
        "region_code": binding.region_code,
        "client_identity_key": binding.client_identity_key,
    }


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _pick(values: tuple[str, ...], digest: str, offset: int) -> str:
    return values[int(digest[offset : offset + 2], 16) % len(values)]
