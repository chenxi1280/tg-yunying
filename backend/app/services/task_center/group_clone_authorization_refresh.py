"""Refresh a stopped clone's control binding after same-account recovery."""
from dataclasses import asdict, dataclass
import hashlib
import json

from sqlalchemy import select

from app.models import Task, TgAccount, TgAccountAuthorization
from app.schemas.task_center import GroupCloneConfig
from app.services._common import audit
from .group_clone_runtime_lifecycle import _assert_close_safe


@dataclass(frozen=True)
class ControlAuthorizationRefresh:
    task_id: str
    tenant_id: int
    epoch: int
    revision: int
    config_hash: str
    account_id: int
    old_authorization_id: int
    new_authorization_id: int


def preview_control_authorization_refresh(session, task_id: str, *, tenant_id: int):
    task = _task(session, task_id, tenant_id=tenant_id, lock=False)
    return _preview(session, task, lock=False)


def apply_control_authorization_refresh(session, expected: ControlAuthorizationRefresh, *, actor: str):
    if not actor.strip():
        raise ValueError('group_clone_refresh_actor_required')
    task = _task(session, expected.task_id, tenant_id=expected.tenant_id, lock=True)
    current = _preview(session, task, lock=True)
    if current != expected:
        raise ValueError('group_clone_control_refresh_preview_changed')
    if current.old_authorization_id == current.new_authorization_id:
        raise ValueError('group_clone_control_refresh_already_current')
    config = dict(task.type_config or {})
    target = {**config['target'], 'control_authorization_id': current.new_authorization_id}
    task.type_config = GroupCloneConfig.model_validate({**config, 'target': target}).model_dump(mode='json')
    task.config_revision = current.revision + 1
    audit(session, tenant_id=task.tenant_id, actor=actor, action='刷新克隆任务同账号控制授权',
          target_type='task', target_id=task.id, detail=json.dumps(asdict(current), sort_keys=True))
    session.flush()
    return task


def _task(session, task_id, *, tenant_id, lock):
    query = select(Task).where(Task.id == task_id, Task.tenant_id == tenant_id,
                               Task.type == 'group_clone', Task.deleted_at.is_(None))
    task = session.scalar(query.with_for_update().execution_options(populate_existing=True) if lock else query)
    if task is None or task.status != 'stopped':
        raise ValueError('group_clone_control_refresh_requires_stopped')
    _assert_close_safe(session, task)
    return task


def _preview(session, task, *, lock):
    config = GroupCloneConfig.model_validate(task.type_config or {})
    target = config.target
    account = _row(session, TgAccount, target.control_account_id, lock=lock)
    if (account is None or account.tenant_id != task.tenant_id or account.deleted_at is not None
            or account.status != 'active'):
        raise ValueError('group_clone_control_account_unavailable')
    old = _row(session, TgAccountAuthorization, target.control_authorization_id, lock=lock)
    new = _row(session, TgAccountAuthorization, account.current_authorization_id, lock=lock)
    _validate_identity(old, new, account)
    digest = hashlib.sha256(json.dumps(task.type_config, sort_keys=True,
                                      separators=(',', ':')).encode()).hexdigest()
    return ControlAuthorizationRefresh(task_id=task.id, tenant_id=task.tenant_id,
        epoch=task.task_lifecycle_epoch, revision=task.config_revision, config_hash=digest,
        account_id=account.id, old_authorization_id=old.id, new_authorization_id=new.id)


def _row(session, model, identity, *, lock):
    query = select(model).where(model.id == identity)
    return session.scalar(query.with_for_update().execution_options(populate_existing=True) if lock else query)


def _validate_identity(old, new, account):
    if old is None or new is None:
        raise ValueError('group_clone_control_authorization_missing')
    if any((auth.account_id, auth.tenant_id) != (account.id, account.tenant_id) for auth in (old, new)):
        raise ValueError('group_clone_control_authorization_scope_mismatch')
    if not _current_authorization_available(new):
        raise ValueError('group_clone_control_authorization_unavailable')
    if not old.telegram_user_id_digest or old.telegram_user_id_digest != new.telegram_user_id_digest:
        raise ValueError('group_clone_control_telegram_identity_mismatch')


def _current_authorization_available(authorization):
    return (authorization.is_current and authorization.is_slot_current
            and authorization.status == 'active' and authorization.health_status == 'healthy'
            and authorization.credential_storage_scope == 'central_business')
