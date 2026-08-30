from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AccountGroupAdmissionFact,
    Action,
    OperationTarget,
    Task,
)
from app.services._common import _now, audit

from .ai_group_rescue_binding_recovery import (
    RecoveryScope,
    account_hash,
    exact_task,
    require_apply_fields,
    require_target_identity,
)
from .ai_group_rescue_admission_projection import (
    complete_member_projection,
    membership_item_for_source,
)
from .channel_membership import mark_channel_membership_joined
from .group_rescue import rescue_admin_account_id_for_task
from .runtime_state_hash import canonical_state_hash


OBSERVATION_OUTCOMES = frozenset({"member", "absent", "inconclusive"})
RECOVERABLE_SOURCE_STATUSES = frozenset({
    "closed_unknown",
    "unknown_after_send",
    "skipped",
})


@dataclass(frozen=True)
class MembershipObservation:
    source_action_id: str
    target_account_id: int
    outcome: str
    evidence_fingerprint: str


def preview_admission_recovery(
    session: Session,
    scope: RecoveryScope,
    observations: tuple[MembershipObservation, ...],
    *,
    lock: bool = False,
) -> dict:
    task = exact_task(session, scope, lock=lock)
    require_target_identity(session, task, scope)
    admin_id = rescue_admin_account_id_for_task(session, task)
    if not admin_id:
        raise ValueError("admission_recovery_admin_missing")
    normalized = _unique_observations(observations)
    snapshots = [
        _observation_snapshot(
            session,
            task,
            scope=scope,
            observation=item,
            lock=lock,
        )
        for item in normalized
    ]
    body = {
        "mode": "preview",
        "deployed_sha": scope.deployed_sha.lower(),
        "task_scope_hash": canonical_state_hash({"tenant": task.tenant_id, "task": task.id}),
        "task_epoch": int(task.task_lifecycle_epoch or 1),
        "task_status": task.status,
        "config_revision": int(task.config_revision or 1),
        "rescue_admin_hash": account_hash(task.tenant_id, admin_id),
        "observations": snapshots,
    }
    return {**body, "fingerprint": canonical_state_hash(body)}


def apply_admission_recovery(
    session: Session,
    scope: RecoveryScope,
    observations: tuple[MembershipObservation, ...],
    *,
    expected_fingerprint: str,
    actor: str,
    approval_reference: str,
) -> dict:
    require_apply_fields(expected_fingerprint, actor, approval_reference)
    preview = preview_admission_recovery(session, scope, observations, lock=True)
    if preview["fingerprint"] != expected_fingerprint:
        raise RuntimeError("admission_recovery_fingerprint_drift")
    task = session.get(Task, scope.task_id)
    counts = {"member": 0, "replacement": 0, "inconclusive": 0}
    for observation in _unique_observations(observations):
        _apply_observation(
            session,
            task,
            scope=scope,
            observation=observation,
            counts=counts,
        )
    _write_admission_audit(
        session,
        task,
        preview=preview,
        counts=counts,
        actor=actor,
        approval_reference=approval_reference,
    )
    session.flush()
    return {
        "mode": "apply",
        "fingerprint": expected_fingerprint,
        "member_count": counts["member"],
        "replacement_count": counts["replacement"],
        "inconclusive_count": counts["inconclusive"],
    }


def _unique_observations(
    observations: tuple[MembershipObservation, ...],
) -> tuple[MembershipObservation, ...]:
    if not observations:
        raise ValueError("admission_recovery_observations_missing")
    source_ids = [item.source_action_id for item in observations]
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("admission_recovery_duplicate_source_action")
    for observation in observations:
        _validate_observation(observation)
    return tuple(sorted(observations, key=lambda item: item.source_action_id))


def _validate_observation(observation: MembershipObservation) -> None:
    if observation.outcome not in OBSERVATION_OUTCOMES:
        raise ValueError("admission_recovery_outcome_invalid")
    if not observation.source_action_id or not observation.evidence_fingerprint:
        raise ValueError("admission_recovery_evidence_missing")
    if observation.target_account_id <= 0:
        raise ValueError("admission_recovery_target_account_invalid")


def _source_rescue_action(
    session: Session,
    task: Task,
    *,
    scope: RecoveryScope,
    observation: MembershipObservation,
    lock: bool,
) -> Action:
    statement = select(Action).where(
        Action.id == observation.source_action_id,
        Action.tenant_id == task.tenant_id,
        Action.task_id == task.id,
        Action.action_type == "invite_group_account",
    )
    if lock:
        statement = statement.with_for_update()
    action = session.scalar(statement)
    if action is None:
        raise ValueError("admission_recovery_source_action_missing")
    _require_source_state(
        action,
        task,
        scope=scope,
        observation=observation,
        session=session,
    )
    return action


def _require_source_state(
    action: Action,
    task: Task,
    *,
    scope: RecoveryScope,
    observation: MembershipObservation,
    session: Session,
) -> None:
    payload = dict(action.payload or {})
    observed = (
        action.status,
        int(action.task_lifecycle_epoch or 1),
        int(payload.get("group_id") or 0),
        int(payload.get("operation_target_id") or 0),
        int(payload.get("target_account_id") or 0),
    )
    expected = (
        action.status,
        scope.expected_epoch,
        scope.expected_group_id,
        scope.expected_target_id,
        observation.target_account_id,
    )
    if action.status not in RECOVERABLE_SOURCE_STATUSES or observed != expected:
        raise RuntimeError("admission_recovery_source_action_drift")
    target = session.get(OperationTarget, scope.expected_target_id)
    if target is None or str(payload.get("group_peer_id")) != str(target.tg_peer_id):
        raise RuntimeError("admission_recovery_source_peer_drift")
    if int(action.account_id or 0) == observation.target_account_id:
        raise RuntimeError("admission_recovery_source_role_invalid")
    if task.tenant_id != action.tenant_id:
        raise RuntimeError("admission_recovery_source_tenant_drift")


def _observation_snapshot(
    session: Session,
    task: Task,
    *,
    scope: RecoveryScope,
    observation: MembershipObservation,
    lock: bool,
) -> dict:
    action = _source_rescue_action(
        session,
        task,
        scope=scope,
        observation=observation,
        lock=lock,
    )
    membership_item = membership_item_for_source(
        session,
        task,
        source_action=action,
        account_id=observation.target_account_id,
        target_id=scope.expected_target_id,
        lock=lock,
    )
    return {
        "source_action_hash": canonical_state_hash({"action": action.id})[:16],
        "target_account_hash": account_hash(task.tenant_id, observation.target_account_id),
        "source_admin_hash": account_hash(task.tenant_id, action.account_id),
        "source_status": action.status,
        "outcome": observation.outcome,
        "evidence_fingerprint": observation.evidence_fingerprint,
        "replacement_exists": _replacement_action(
            session,
            task.tenant_id,
            _replacement_key(task, observation),
        ) is not None,
        "membership_phase": membership_item.phase,
        "membership_rescue_status": membership_item.rescue_status,
        "membership_rescue_action_hash": canonical_state_hash({
            "action": membership_item.rescue_action_id or "",
        })[:16],
    }


def _replacement_key(task: Task, observation: MembershipObservation) -> str:
    identity = canonical_state_hash({
        "source_action_id": observation.source_action_id,
        "task_epoch": int(task.task_lifecycle_epoch or 1),
        "evidence_fingerprint": observation.evidence_fingerprint,
    })
    return f"group-rescue-replacement:{identity}"


def _replacement_action(
    session: Session,
    tenant_id: int,
    dedupe_key: str,
) -> Action | None:
    return session.scalar(select(Action).where(
        Action.tenant_id == tenant_id,
        Action.action_dedupe_key == dedupe_key,
    ))


def _apply_observation(
    session: Session,
    task: Task,
    *,
    scope: RecoveryScope,
    observation: MembershipObservation,
    counts: dict[str, int],
) -> None:
    action = _source_rescue_action(
        session,
        task,
        scope=scope,
        observation=observation,
        lock=True,
    )
    if observation.outcome == "inconclusive":
        counts["inconclusive"] += 1
        return
    if observation.outcome == "member":
        _apply_member_observation(
            session,
            task,
            scope=scope,
            action=action,
            observation=observation,
        )
        counts["member"] += 1
        return
    if _create_replacement(
        session,
        task,
        scope=scope,
        source_action=action,
        observation=observation,
    ):
        counts["replacement"] += 1


def _apply_member_observation(
    session: Session,
    task: Task,
    *,
    scope: RecoveryScope,
    action: Action,
    observation: MembershipObservation,
) -> None:
    fact_hash = canonical_state_hash({
        "source_action_id": action.id,
        "target_account_id": observation.target_account_id,
        "evidence_fingerprint": observation.evidence_fingerprint,
    })
    existing = session.scalar(select(AccountGroupAdmissionFact).where(
        AccountGroupAdmissionFact.account_id == observation.target_account_id,
        AccountGroupAdmissionFact.target_group_id == scope.expected_group_id,
        AccountGroupAdmissionFact.fact_kind == "membership_observed",
        AccountGroupAdmissionFact.fact_identity_hash == fact_hash,
    ))
    if existing is None:
        session.add(AccountGroupAdmissionFact(
            tenant_id=task.tenant_id,
            account_id=observation.target_account_id,
            target_group_id=scope.expected_group_id,
            fact_kind="membership_observed",
            fact_identity_hash=fact_hash,
            outcome={"source": "group_rescue_read_only_reconcile"},
        ))
    mark_channel_membership_joined(
        session,
        task.tenant_id,
        scope.expected_target_id,
        observation.target_account_id,
    )
    complete_member_projection(
        session,
        task,
        source_action=action,
        account_id=observation.target_account_id,
        target_id=scope.expected_target_id,
        group_id=scope.expected_group_id,
    )


def _create_replacement(
    session: Session,
    task: Task,
    *,
    scope: RecoveryScope,
    source_action: Action,
    observation: MembershipObservation,
) -> bool:
    dedupe_key = _replacement_key(task, observation)
    if _replacement_action(session, task.tenant_id, dedupe_key) is not None:
        return False
    admin_id = rescue_admin_account_id_for_task(session, task)
    if not admin_id:
        raise ValueError("admission_recovery_admin_missing")
    replacement = Action(
        tenant_id=task.tenant_id,
        task_id=task.id,
        task_type=task.type,
        action_type="invite_group_account",
        account_id=admin_id,
        scheduled_at=_now(),
        status="pending",
        task_lifecycle_epoch=int(task.task_lifecycle_epoch or 1),
        payload=dict(source_action.payload or {}),
        result={
            "rescue_status": "pending",
            "recovery_source": "remote_absence",
            "recovery_source_action_id": source_action.id,
            "recovery_evidence_fingerprint": observation.evidence_fingerprint,
        },
        action_dedupe_key=dedupe_key,
    )
    session.add(replacement)
    session.flush()
    item = membership_item_for_source(
        session,
        task,
        source_action=source_action,
        account_id=observation.target_account_id,
        target_id=scope.expected_target_id,
    )
    item.rescue_action_id = replacement.id
    item.rescue_status = "pending"
    item.rescue_failure_detail = ""
    item.updated_at = _now()
    return True


def _write_admission_audit(
    session: Session,
    task: Task,
    *,
    preview: dict,
    counts: dict[str, int],
    actor: str,
    approval_reference: str,
) -> None:
    audit(
        session,
        tenant_id=task.tenant_id,
        actor=actor,
        action="受保护核对AI活群救援准入",
        target_type="task",
        target_id=task.id,
        detail=(
            f"approval={approval_reference};fingerprint={preview['fingerprint']};"
            f"member={counts['member']};replacement={counts['replacement']};"
            f"inconclusive={counts['inconclusive']}"
        ),
    )


__all__ = [
    "MembershipObservation",
    "apply_admission_recovery",
    "preview_admission_recovery",
]
