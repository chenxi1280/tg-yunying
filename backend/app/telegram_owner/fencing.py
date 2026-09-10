"""Read current authorization facts before issuing a caller's frozen credentials."""
from __future__ import annotations

from app.models import TgAccount, TgAccountAuthorization, TelegramDeveloperApp
from app.security import decrypt_session, decrypt_secret

from .session_identity import authorization_identity
from .errors import TelegramOwnerRequestRejected


def validate_credentials(session_factory, credentials, identity: str | None):
    if credentials is None:
        return
    with session_factory() as session:
        _require_developer_app(session, credentials)
        if credentials.account_id is None:
            return
        account = session.get(TgAccount, credentials.account_id)
        _require_account_generation(account, credentials)
        if credentials.authorization_id is None:
            _require_session_identity(account.session_ciphertext, identity)
            return
        authorization = session.get(TgAccountAuthorization, credentials.authorization_id)
        _require_authorization(authorization, credentials)
        _require_session_identity(authorization.session_ciphertext, identity)


def _require_account_generation(account, credentials):
    valid = (account is not None and account.deleted_at is None
             and account.tenant_id == credentials.tenant_id
             and account.authorization_generation == credentials.authorization_generation
             and account.connection_generation == credentials.connection_generation)
    if not valid:
        raise TelegramOwnerRequestRejected("telegram_owner_account_generation_changed")


def _require_authorization(authorization, credentials):
    valid = (authorization is not None and authorization.account_id == credentials.account_id
             and authorization.tenant_id == credentials.tenant_id
             and authorization.disabled_at is None and authorization.provision_region_code == "sv"
             and authorization.health_status != "invalid"
             and authorization.developer_app_id == credentials.app_id)
    if not valid:
        raise TelegramOwnerRequestRejected("telegram_owner_authorization_identity_changed")
    if credentials.authorization_fact_version is not None:
        if credentials.authorization_fact_version != authorization.fact_version:
            raise TelegramOwnerRequestRejected("telegram_owner_authorization_fact_changed")


def _require_session_identity(ciphertext, identity):
    if identity is None:
        return
    current = decrypt_session(ciphertext)
    if not current or authorization_identity(current) != identity:
        raise TelegramOwnerRequestRejected("telegram_owner_session_identity_changed")


def _require_developer_app(session, credentials):
    if credentials.app_id is None:
        return
    app = session.get(TelegramDeveloperApp, credentials.app_id)
    valid = (app is not None and app.is_active and app.api_id == credentials.api_id
             and app.credentials_version == credentials.credentials_version
             and decrypt_secret(app.api_hash_ciphertext) == credentials.api_hash)
    if not valid:
        raise TelegramOwnerRequestRejected("telegram_owner_developer_app_changed")
