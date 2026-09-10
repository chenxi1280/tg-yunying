from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountStatus, TgAccount, TgAccountAuthorization, TgLoginFlow
from app.integrations.telegram.contracts import AccountAuthorizationSnapshot, AuthorizationIdentity
from app.integrations.telegram.authorization_fingerprint import authorization_fingerprint_digest
from app.security import decrypt_secret, encrypt_secret
from app.services import account_authorizations, account_authorization_metadata
from app.services._common import _now
from app.services.authorization_dr import pending_login_registration as recovery
from app.services.authorization_dr.contracts import AuthorizationDrError
from tests.owner_postgres_support import owner_engine
from test_authorization_abc_backup import _seed

pytestmark = pytest.mark.isolated_postgres


@pytest.fixture
def setup(owner_engine, monkeypatch):
    with Session(owner_engine) as db:
        _seed(db)
        account = db.get(TgAccount, 101)
        current = db.get(TgAccountAuthorization, account.current_authorization_id)
        current.health_status = 'invalid'
        current.telegram_user_id_digest = ''
        flows = [TgLoginFlow(tenant_id=1, account_id=101, method='code', status=AccountStatus.WAITING_CODE.value,
                  authorization_role='standby_1', developer_app_id=2, challenge_sent_at=_now()-timedelta(hours=1),
                  code_expires_at=_now()-timedelta(minutes=55), temporary_session_ciphertext=encrypt_secret(raw))
                 for raw in ['target', 'observer']]
        db.add_all(flows)
        db.commit()
        snapshot = AccountAuthorizationSnapshot(authorization_hash='88', is_current=False, device_model='test',
                    platform='linux', system_version='1', api_id=1002, app_name='B', app_version='1')
        identity = AuthorizationIdentity('0', 'target-key', 'same-user', authorization_fingerprint_digest(snapshot))
        calls = []
        def read_identity(raw, credentials):
            calls.append(raw)
            return identity if raw == 'target' else replace(identity, auth_key_fingerprint_digest='peer-key')
        gateway = SimpleNamespace(authorization_identity=read_identity,
                                  list_authorizations=lambda *_: [snapshot])
        monkeypatch.setattr(recovery, 'credentials_for_developer_app', lambda app: SimpleNamespace(api_id=app.api_id))
        selection = recovery.PendingLoginSelection(tenant_id=1, account_id=101,
                    target_flow_id=flows[0].id, observer_flow_id=flows[1].id)
        yield db, selection, gateway, calls, identity, snapshot


def test_expired_code_with_authorized_session_registers_without_login_and_preserves_current(setup):
    db, selection, gateway, calls, _, _ = setup
    account = db.get(TgAccount, 101)
    before = (account.current_authorization_id, account.session_ciphertext, account.authorization_generation)
    proof = recovery.preview_pending_login_registration(db, selection, telegram_gateway=gateway)
    approval = recovery.RegistrationApproval(expected_fingerprint=proof['fingerprint'], actor='tester', approval_ref='approved')
    asset = recovery.apply_pending_login_registration(db, selection, approval, telegram_gateway=gateway)
    assert calls == ['target', 'observer', 'target', 'observer']
    assert decrypt_secret(asset.telegram_authorization_hash_ciphertext) == '88'
    assert asset.telegram_user_id_digest == 'same-user' and asset.auth_key_fingerprint_digest == 'target-key'
    assert not asset.is_current and asset.logical_slot == 'standby_1'
    assert (account.current_authorization_id, account.session_ciphertext, account.authorization_generation) == before
    assert db.get(TgLoginFlow, selection.target_flow_id).temporary_session_ciphertext is None
    assert decrypt_secret(db.get(TgLoginFlow, selection.observer_flow_id).temporary_session_ciphertext) == 'observer'
    with pytest.raises(AuthorizationDrError, match='unregistered'):
        recovery.apply_pending_login_registration(db, selection, approval, telegram_gateway=gateway)
    assert len(list(db.scalars(select(TgAccountAuthorization)))) == 2


@pytest.mark.parametrize('failure', ['different_user', 'same_key', 'zero_hash', 'ambiguous', 'current_user'])
def test_unproved_identity_or_hash_rejected_without_registration(setup, failure):
    db, selection, gateway, calls, identity, snapshot = setup
    if failure in {'different_user', 'same_key'}:
        peer = replace(identity, telegram_user_id_digest='other') if failure == 'different_user' else identity
        gateway.authorization_identity = lambda raw, credentials: identity if raw == 'target' else peer
    if failure == 'zero_hash':
        gateway.list_authorizations = lambda *_: [replace(snapshot, authorization_hash='0')]
    if failure == 'ambiguous':
        gateway.list_authorizations = lambda *_: [snapshot, snapshot]
    if failure == 'current_user':
        account = db.get(TgAccount, 101)
        db.get(TgAccountAuthorization, account.current_authorization_id).telegram_user_id_digest = 'different'
    with pytest.raises(AuthorizationDrError):
        recovery.preview_pending_login_registration(db, selection, telegram_gateway=gateway)
    assert len(list(db.scalars(select(TgAccountAuthorization)))) == 1
    assert db.get(TgLoginFlow, selection.target_flow_id).temporary_session_ciphertext


@pytest.mark.parametrize('drift', ['generation', 'flow_version', 'peer_material'])
def test_apply_rejects_changed_frozen_inputs(setup, drift):
    db, selection, gateway, _, _, _ = setup
    proof = recovery.preview_pending_login_registration(db, selection, telegram_gateway=gateway)
    if drift == 'generation':
        db.get(TgAccount, 101).connection_generation += 1
    elif drift == 'flow_version':
        db.get(TgLoginFlow, selection.target_flow_id).flow_version += 1
    else:
        db.get(TgLoginFlow, selection.observer_flow_id).temporary_session_ciphertext = encrypt_secret('observer')
    db.commit()
    approval = recovery.RegistrationApproval(expected_fingerprint=proof['fingerprint'], actor='tester', approval_ref='approved')
    with pytest.raises(AuthorizationDrError, match='proof changed'):
        recovery.apply_pending_login_registration(db, selection, approval, telegram_gateway=gateway)


def test_remote_login_material_survives_metadata_failure(setup, monkeypatch):
    db, selection, _, _, _, _ = setup
    def missing(*args):
        raise ValueError('current authorization hash missing')
    monkeypatch.setattr(account_authorizations, '_current_authorization_hash_after_login', missing)
    flow = db.get(TgLoginFlow, selection.target_flow_id)
    with pytest.raises(ValueError, match='hash missing'):
        account_authorizations._finish_standby_login(db, db.get(TgAccount, 101), flow,
                                                    AccountStatus.ACTIVE.value, 'returned-session', 'tester')
    db.rollback()
    db.expire_all()
    flow = db.get(TgLoginFlow, selection.target_flow_id)
    assert flow.status == AccountStatus.ACTIVE.value and not flow.authorization_id
    assert decrypt_secret(flow.temporary_session_ciphertext) == 'returned-session'


def test_invalid_asset_never_uses_compatibility_session_as_observer(setup, monkeypatch):
    db, _, _, _, _, _ = setup
    calls = []
    monkeypatch.setattr(account_authorization_metadata.gateway, 'list_authorizations', lambda *args: calls.append(args))
    assert list(account_authorization_metadata._peer_authorization_views(db, db.get(TgAccount, 101), None)) == []
    assert calls == []


def test_healthy_registered_original_flow_can_observe_pending_flow(setup, monkeypatch):
    db, selection, gateway, _, _, _ = setup
    account = db.get(TgAccount, 101)
    peer = db.get(TgAccountAuthorization, account.current_authorization_id)
    from app.security import encrypt_session
    peer.session_ciphertext = encrypt_session('observer')
    peer.developer_app_id = 2
    peer.health_status = 'healthy'
    peer.telegram_user_id_digest = 'same-user'
    peer.auth_key_fingerprint_digest = 'peer-key'
    flow = db.get(TgLoginFlow, selection.observer_flow_id)
    flow.authorization_id = peer.id
    flow.temporary_session_ciphertext = None
    db.commit()
    monkeypatch.setattr(recovery, 'credentials_for_authorization', lambda *_: SimpleNamespace())
    proof = recovery.preview_pending_login_registration(db, selection, telegram_gateway=gateway)
    assert proof['observer_asset'][0] == peer.id
    approval = recovery.RegistrationApproval(expected_fingerprint=proof['fingerprint'], actor='tester', approval_ref='approved')
    asset = recovery.apply_pending_login_registration(db, selection, approval, telegram_gateway=gateway)
    assert decrypt_secret(asset.telegram_authorization_hash_ciphertext) == '88'
    assert account.current_authorization_id == peer.id


def test_foreign_flow_is_rejected_before_gateway(setup):
    db, selection, gateway, calls, _, _ = setup
    flow = db.get(TgLoginFlow, selection.observer_flow_id)
    flow.tenant_id = 999
    with db.no_autoflush, pytest.raises(AuthorizationDrError, match='does not belong'):
        recovery.preview_pending_login_registration(db, selection, telegram_gateway=gateway)
    assert calls == []
