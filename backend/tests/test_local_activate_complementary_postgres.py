import pytest
from sqlalchemy.orm import Session

from tests.owner_postgres_support import owner_engine

pytestmark = pytest.mark.isolated_postgres
from app.models import TgAccount
from app.services.authorization_dr import apply_local_activate, preview_local_activate
from app.services.authorization_dr.contracts import AuthorizationDrError
from test_authorization_dr_local_activate_verification import _identity
from tests.local_activate_postgres_support import seed_local_activation


def _preview(monkeypatch, session):
    account, primary, target = seed_local_activation(session)
    primary.logical_slot = 'standby_1'
    target.logical_slot = 'primary'
    session.commit()
    monkeypatch.setattr('app.services.authorization_dr.local_activate._probe_target', lambda *args: _identity())
    monkeypatch.setattr('app.services.authorization_dr.local_activate._current_credentials', lambda *args: None)
    monkeypatch.setattr('app.services.authorization_dr.local_activate.gateway.invalidate_session_cache', lambda *args: 0)
    case = preview_local_activate(session, 1, account.id, target.id, actor='requester', reason='complementary slot')
    return account, target, case


def _apply(session, account, target, case):
    return apply_local_activate(session, 1, account.id, target.id, fingerprint=case.fingerprint,
                                actor='executor', approval_ref='test-user-request', idempotency_key='test-activation')


def test_complementary_sv_slot_switches_without_renaming_physical_slot(monkeypatch, owner_engine):
    engine = owner_engine
    with Session(engine) as session:
        account, target, case = _preview(monkeypatch, session)
        _apply(session, account, target, case)
        session.refresh(account)
        session.refresh(target)
        assert account.current_authorization_id == target.id
        assert target.logical_slot == 'primary'
        assert target.role == 'primary'
        assert case.status == 'applied_pending_verification'


def test_apply_refreshes_locked_state_after_concurrent_generation_change(monkeypatch, owner_engine):
    engine = owner_engine
    with Session(engine) as session:
        account, target, case = _preview(monkeypatch, session)
        original_generation = account.connection_generation
        with Session(engine) as other:
            fresh = other.get(TgAccount, account.id)
            fresh.connection_generation += 1
            other.commit()
        assert account.connection_generation == original_generation
        with pytest.raises(AuthorizationDrError):
            _apply(session, account, target, case)
        session.rollback()
        session.refresh(account)
        assert account.current_authorization_id != target.id
        assert account.connection_generation == original_generation + 1


@pytest.mark.parametrize("has_digests", [True, False])
def test_local_activate_preserves_peer_hash_when_same_identity_self_hash_is_zero(monkeypatch, owner_engine, has_digests):
    from dataclasses import replace
    from app.security import encrypt_secret, decrypt_secret
    from app.services.authorization_dr.local_activate import _apply_probed_identity
    with Session(owner_engine) as session:
        _, _, target = seed_local_activation(session)
        identity = _identity()
        target.telegram_user_id_digest = identity.telegram_user_id_digest if has_digests else ""
        target.auth_key_fingerprint_digest = identity.auth_key_fingerprint_digest if has_digests else ""
        target.telegram_authorization_hash_ciphertext = encrypt_secret('proved-peer-hash')
        _apply_probed_identity(target, replace(identity, authorization_hash='0'))
        assert decrypt_secret(target.telegram_authorization_hash_ciphertext) == 'proved-peer-hash'


def test_local_activate_does_not_reuse_hash_from_different_authkey(monkeypatch, owner_engine):
    from dataclasses import replace
    from app.security import encrypt_secret
    from app.services.authorization_dr.local_activate import _apply_probed_identity
    with Session(owner_engine) as session:
        _, _, target = seed_local_activation(session)
        target.auth_key_fingerprint_digest = 'old-key'
        target.telegram_authorization_hash_ciphertext = encrypt_secret('old-peer-hash')
        with pytest.raises(AuthorizationDrError, match='no proved device hash'):
            _apply_probed_identity(target, replace(_identity(), authorization_hash='0'))
