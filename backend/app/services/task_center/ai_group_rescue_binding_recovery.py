from __future__ import annotations

from dataclasses import asdict, dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AccountStatus, Action, OperationTarget, Task, TgAccount, TgGroup
from app.services._common import audit

from .runtime_state_hash import canonical_state_hash


OPEN_ACTION_STATUSES = frozenset({
    "pending",
    "claiming",
    "executing",
    "retryable_failed",
    "unknown_after_send",
})


@dataclass(frozen=True)
class RecoveryScope:
    task_id: str
    expected_epoch: int
    expected_config_revision: int
    expected_target_id: int
    expected_group_id: int
    deployed_sha: str


@dataclass(frozen=True)
class BindingEvidence:
    rescue_admin_account_id: int
    listener_account_id: int
    target_peer_digest: str
    rescue_identity_digest: str
    listener_identity_digest: str
    rescue_is_admin: bool
    invite_users: bool
    ban_users: bool
    delete_messages: bool
    listener_is_member: bool
    listener_history_readable: bool


def preview_binding_recovery(
    session: Session,
    scope: RecoveryScope,
    evidence: BindingEvidence,
    *,
    lock: bool = False,
) -> dict:
    task = exact_task(session, scope, lock=lock)
    require_target_identity(session, task, scope)
    _require_accounts(session, task, evidence)
    _require_remote_evidence(evidence)
    open_count = _open_action_count(session, evidence.rescue_admin_account_id)
    if open_count:
        raise ValueError("rescue_admin_open_action")
    body = _preview_body(
        task,
        scope,
        evidence=evidence,
        open_count=open_count,
    )
    return {**body, "fingerprint": canonical_state_hash(body)}


def apply_binding_recovery(
    session: Session,
    scope: RecoveryScope,
    evidence: BindingEvidence,
    *,
    expected_fingerprint: str,
    actor: str,
    approval_reference: str,
) -> dict:
    require_apply_fields(expected_fingerprint, actor, approval_reference)
    preview = preview_binding_recovery(session, scope, evidence, lock=True)
    if preview["fingerprint"] != expected_fingerprint:
        raise RuntimeError("binding_recovery_fingerprint_drift")
    task = session.get(Task, scope.task_id)
    config = dict(task.type_config or {})
    config["group_rescue_admin_account_id"] = evidence.rescue_admin_account_id
    config["history_fetch_account_id"] = evidence.listener_account_id
    task.type_config = config
    task.config_revision = int(task.config_revision or 1) + 1
    _write_audit(
        session,
        task,
        preview=preview,
        actor=actor,
        approval_reference=approval_reference,
    )
    session.flush()
    return _readback(task, evidence, expected_fingerprint)


def exact_task(
    session: Session,
    scope: RecoveryScope,
    *,
    lock: bool,
) -> Task:
    statement = select(Task).where(
        Task.id == scope.task_id,
        Task.type == "group_ai_chat",
        Task.deleted_at.is_(None),
    )
    if lock:
        statement = statement.with_for_update()
    task = session.scalar(statement)
    if task is None:
        raise ValueError("exact_group_ai_task_not_found")
    observed = (int(task.task_lifecycle_epoch or 1), int(task.config_revision or 1))
    if observed != (scope.expected_epoch, scope.expected_config_revision):
        raise RuntimeError("binding_recovery_task_state_drift")
    return task


def require_target_identity(
    session: Session,
    task: Task,
    scope: RecoveryScope,
) -> None:
    config = dict(task.type_config or {})
    observed = (
        int(config.get("target_operation_target_id") or 0),
        int(config.get("target_group_id") or 0),
    )
    if observed != (scope.expected_target_id, scope.expected_group_id):
        raise RuntimeError("binding_recovery_target_state_drift")
    group = session.get(TgGroup, scope.expected_group_id)
    target = session.get(OperationTarget, scope.expected_target_id)
    if group is None or group.tenant_id != task.tenant_id:
        raise ValueError("binding_recovery_group_missing")
    if target is None or target.tenant_id != task.tenant_id:
        raise ValueError("binding_recovery_target_missing")
    if str(group.tg_peer_id) != str(target.tg_peer_id):
        raise RuntimeError("binding_recovery_target_peer_mismatch")


def require_apply_fields(
    fingerprint: str,
    actor: str,
    approval_reference: str,
) -> None:
    if not fingerprint or not actor.strip() or not approval_reference.strip():
        raise ValueError("binding_recovery_apply_fields_missing")


def account_hash(tenant_id: int, account_id: object) -> str:
    return canonical_state_hash({
        "tenant_id": tenant_id,
        "account_id": int(account_id or 0),
    })[:16]


def _require_accounts(
    session: Session,
    task: Task,
    evidence: BindingEvidence,
) -> None:
    if evidence.rescue_admin_account_id == evidence.listener_account_id:
        raise ValueError("binding_recovery_roles_must_be_distinct")
    account_ids = (
        evidence.rescue_admin_account_id,
        evidence.listener_account_id,
    )
    for account_id in account_ids:
        account = session.get(TgAccount, account_id)
        if not _account_is_usable(account, task.tenant_id):
            raise ValueError("binding_recovery_account_unavailable")


def _account_is_usable(account: TgAccount | None, tenant_id: int) -> bool:
    return bool(
        account
        and account.tenant_id == tenant_id
        and account.deleted_at is None
        and account.status == AccountStatus.ACTIVE.value
        and account.session_ciphertext
    )


def _require_remote_evidence(evidence: BindingEvidence) -> None:
    identities = (
        evidence.target_peer_digest,
        evidence.rescue_identity_digest,
        evidence.listener_identity_digest,
    )
    if not all(identities):
        raise ValueError("binding_recovery_remote_identity_missing")
    if evidence.rescue_identity_digest == evidence.listener_identity_digest:
        raise ValueError("binding_recovery_remote_identity_duplicate")
    capabilities = (
        evidence.rescue_is_admin,
        evidence.invite_users,
        evidence.ban_users,
        evidence.delete_messages,
        evidence.listener_is_member,
        evidence.listener_history_readable,
    )
    if not all(capabilities):
        raise ValueError("binding_recovery_remote_capability_missing")


def _open_action_count(session: Session, account_id: int) -> int:
    return int(session.scalar(select(func.count(Action.id)).where(
        Action.account_id == account_id,
        Action.status.in_(OPEN_ACTION_STATUSES),
    )) or 0)


def _preview_body(
    task: Task,
    scope: RecoveryScope,
    *,
    evidence: BindingEvidence,
    open_count: int,
) -> dict:
    config = dict(task.type_config or {})
    return {
        "mode": "preview",
        "deployed_sha": scope.deployed_sha.lower(),
        "task_scope_hash": canonical_state_hash({"tenant": task.tenant_id, "task": task.id}),
        "task_epoch": int(task.task_lifecycle_epoch or 1),
        "task_status": task.status,
        "config_revision": int(task.config_revision or 1),
        "target_id": scope.expected_target_id,
        "group_id": scope.expected_group_id,
        "old_rescue_admin_hash": account_hash(task.tenant_id, config.get("group_rescue_admin_account_id")),
        "old_listener_hash": account_hash(task.tenant_id, config.get("history_fetch_account_id")),
        "new_rescue_admin_hash": account_hash(task.tenant_id, evidence.rescue_admin_account_id),
        "new_listener_hash": account_hash(task.tenant_id, evidence.listener_account_id),
        "rescue_admin_open_action_count": open_count,
        "remote_evidence": _remote_evidence_body(evidence),
    }


def _remote_evidence_body(evidence: BindingEvidence) -> dict:
    body = asdict(evidence)
    body.pop("rescue_admin_account_id")
    body.pop("listener_account_id")
    return body


def _write_audit(
    session: Session,
    task: Task,
    *,
    preview: dict,
    actor: str,
    approval_reference: str,
) -> None:
    audit(
        session,
        tenant_id=task.tenant_id,
        actor=actor,
        action="受保护修复AI活群救援与监听绑定",
        target_type="task",
        target_id=task.id,
        detail=f"approval={approval_reference};fingerprint={preview['fingerprint']}",
    )


def _readback(
    task: Task,
    evidence: BindingEvidence,
    fingerprint: str,
) -> dict:
    config = dict(task.type_config or {})
    if int(config.get("group_rescue_admin_account_id") or 0) != evidence.rescue_admin_account_id:
        raise RuntimeError("binding_recovery_admin_readback_mismatch")
    if int(config.get("history_fetch_account_id") or 0) != evidence.listener_account_id:
        raise RuntimeError("binding_recovery_listener_readback_mismatch")
    return {
        "mode": "apply",
        "fingerprint": fingerprint,
        "config_revision": int(task.config_revision or 1),
        "rescue_admin_hash": account_hash(task.tenant_id, evidence.rescue_admin_account_id),
        "listener_hash": account_hash(task.tenant_id, evidence.listener_account_id),
    }


__all__ = [
    "BindingEvidence",
    "RecoveryScope",
    "account_hash",
    "apply_binding_recovery",
    "exact_task",
    "preview_binding_recovery",
    "require_apply_fields",
    "require_target_identity",
]
