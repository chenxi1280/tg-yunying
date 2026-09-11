import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountStatus, Action, AuditLog, Task, Tenant, TgAccount, TgAccountAuthorization
from app.schemas.task_center import GroupCloneConfig
from app.services.task_center.group_clone_authorization_refresh import (
    apply_control_authorization_refresh, preview_control_authorization_refresh,
)
from tests.owner_postgres_support import owner_engine

pytestmark = pytest.mark.isolated_postgres


def _config():
    return GroupCloneConfig.model_validate({
        'source': {'internal_group_id': 21, 'operation_target_id': 11, 'peer_type': 'channel',
                   'peer_id': '-100111', 'listener_account_id': 101, 'authorization_id': 201,
                   'authorization_mode': 'admin_authorized'},
        'target': {'internal_group_id': 22, 'operation_target_id': 12, 'peer_type': 'channel',
                   'peer_id': '-100222', 'control_account_id': 102, 'control_authorization_id': 202},
        'sender_pool': {'account_ids': [101, 102]},
        'content': {'rule_set_id': 31, 'rule_set_version': 1},
    }).model_dump(mode='json')


@pytest.fixture
def scope(owner_engine):
    with Session(owner_engine) as db:
        db.add(Tenant(id=1, name='clone'))
        db.flush()
        account = TgAccount(id=102, tenant_id=1, display_name='control', phone_masked='test-control', status=AccountStatus.ACTIVE.value)
        db.add(account)
        db.flush()
        old = TgAccountAuthorization(id=202, tenant_id=1, account_id=102, is_current=False,
            is_slot_current=False, health_status='invalid', telegram_user_id_digest='same-user')
        new = TgAccountAuthorization(id=203, tenant_id=1, account_id=102, is_current=True,
            health_status='healthy', telegram_user_id_digest='same-user')
        db.add_all([old, new])
        db.flush()
        account.current_authorization_id = new.id
        task = Task(id='clone', tenant_id=1, name='clone', type='group_clone', status='stopped',
                    task_lifecycle_epoch=3, config_revision=1, type_config=_config())
        db.add(task)
        db.commit()
        yield db, task, account, old, new


def test_refresh_preserves_identity_and_old_authorization_with_audit(scope):
    db, task, account, old, new = scope
    before = json.dumps(task.type_config, sort_keys=True)
    preview = preview_control_authorization_refresh(db, task.id, tenant_id=1)
    assert json.dumps(task.type_config, sort_keys=True) == before
    apply_control_authorization_refresh(db, preview, actor='operator')
    db.commit()
    expected = json.loads(before)
    expected['target']['control_authorization_id'] = new.id
    assert task.type_config == expected and task.config_revision == 2
    assert task.task_lifecycle_epoch == 3 and task.status == 'stopped'
    assert old.health_status == 'invalid' and not old.is_current
    assert account.current_authorization_id == new.id
    assert db.scalar(select(AuditLog).where(AuditLog.target_id == task.id)) is not None


@pytest.mark.parametrize('conflict', ['revision', 'digest', 'health', 'account_status', 'running', 'unknown'])
def test_refresh_rejects_drift_and_unresolved_execution(scope, conflict):
    db, task, account, old, new = scope
    preview = preview_control_authorization_refresh(db, task.id, tenant_id=1)
    if conflict == 'revision':
        task.config_revision += 1
    if conflict == 'digest':
        new.telegram_user_id_digest = 'different-user'
    if conflict == 'health':
        new.health_status = 'invalid'
    if conflict == 'account_status':
        account.status = AccountStatus.DISABLED.value
    if conflict == 'running':
        task.status = 'running'
    if conflict == 'unknown':
        db.add(Action(task_id=task.id, tenant_id=1, task_type='group_clone',
                      action_type='send_message', status='unknown_after_send'))
    db.commit()
    with pytest.raises(ValueError):
        apply_control_authorization_refresh(db, preview, actor='operator')
    db.rollback()
    assert task.type_config['target']['control_authorization_id'] == old.id
