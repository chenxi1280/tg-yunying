from __future__ import annotations

from app.models import TgAccount, TgAccountAuthorization

from .contracts import AuthorizationDrError


def require_primary_code_source(account: TgAccount) -> TgAccountAuthorization:
    source = next((row for row in account.authorizations if row.id == account.current_authorization_id), None)
    valid = (
        source
        and source.logical_slot in {"primary", "standby_1"}
        and source.is_current
        and source.provision_region_code == "sv"
        and source.session_ciphertext == account.session_ciphertext
        and source.developer_app_id == account.developer_app_id
        and source.telegram_user_id_digest
        and source.auth_key_fingerprint_digest
    )
    if not valid:
        raise AuthorizationDrError("primary_canonical_unproven", "Current A authorization is not canonical")
    return source


def verified_code_source(session, operation) -> TgAccountAuthorization:
    account = session.get(TgAccount, operation.account_id)
    source = session.get(TgAccountAuthorization, operation.code_source_authorization_id)
    if not account or not source or not _matches_frozen_primary(account, source, operation):
        raise AuthorizationDrError("code_source_changed", "Frozen A authorization changed")
    return source


def _matches_frozen_primary(account, source, operation) -> bool:
    return (
        account.current_authorization_id == operation.expected_current_authorization_id == source.id
        and account.authorization_generation == operation.expected_authorization_generation
        and account.authorization_fact_generation == operation.expected_authorization_fact_generation
        and account.connection_generation == operation.expected_connection_generation
        and account.session_ciphertext == source.session_ciphertext
        and account.developer_app_id == source.developer_app_id
        and source.fact_version == operation.expected_code_source_fact_version
        and source.telegram_user_id_digest == operation.expected_code_source_user_id_digest
        and source.auth_key_fingerprint_digest == operation.expected_code_source_auth_key_digest
        and source.logical_slot in {"primary", "standby_1"}
        and source.is_current
        and source.provision_region_code == "sv"
    )


__all__ = ["require_primary_code_source", "verified_code_source"]
