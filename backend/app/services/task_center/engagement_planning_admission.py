from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import (
    AccountStatus,
    AiAccountVoiceProfile,
    OperationTarget,
    PlanningAdmissionSnapshot,
    Task,
    TaskMembershipAdmissionItem,
    TaskParticipationUnitPlan,
    TgAccount,
    TgAccountAuthorization,
)
from app.services._common import _now

from .channel_membership import channel_member_accounts
from .comment_account_profiles import comment_account_profile_ready


OBSERVATION_TTL = timedelta(minutes=5)
CONTENT_TASK_TYPES = frozenset({"group_ai_chat", "channel_comment"})


def ensure_planning_admission_snapshot(
    session: Session,
    task: Task,
    participation: TaskParticipationUnitPlan,
    *,
    planning_horizon: str,
    target: OperationTarget | None = None,
    require_send: bool = False,
    now: datetime | None = None,
    candidate_account_ids: list[int] | None = None,
) -> PlanningAdmissionSnapshot:
    observed_at = now or _now()
    account_ids = _admission_account_ids(participation, candidate_account_ids)
    paths = _planning_paths(
        session,
        task,
        account_ids,
        target=target,
        require_send=require_send,
    )
    admissible, deficits, decision_hash = _decision_payload(paths)
    existing = _snapshot(
        session,
        task,
        participation,
        horizon=planning_horizon,
        decision_hash=decision_hash,
        observed_at=observed_at,
    )
    if existing is not None:
        _project_snapshot(task, existing)
        return existing
    dependency_hash = _observation_dependency_hash(paths, observed_at)
    snapshot = _new_snapshot(
        task,
        participation,
        planning_horizon=planning_horizon,
        dependency_hash=dependency_hash,
        paths=paths,
        admissible=admissible,
        deficits=deficits,
        valid_until=observed_at + OBSERVATION_TTL,
        decision_hash=decision_hash,
    )
    session.add(snapshot)
    session.flush()
    _project_snapshot(task, snapshot)
    return snapshot


def _admission_account_ids(
    participation: TaskParticipationUnitPlan,
    candidate_account_ids: list[int] | None,
) -> list[int]:
    if candidate_account_ids is None:
        return [int(item) for item in participation.selected_account_ids or []]
    account_ids = list(dict.fromkeys(int(item) for item in candidate_account_ids))
    eligible = {int(item) for item in participation.policy_eligible_account_ids or []}
    if not set(account_ids) <= eligible:
        raise ValueError("planning_admission_candidate_outside_frozen_membership")
    return account_ids


def _decision_payload(paths: list[dict]) -> tuple[list[int], list[int], str]:
    admissible = [int(path["account_id"]) for path in paths if path["admissible"]]
    deficits = [int(path["account_id"]) for path in paths if not path["admissible"]]
    decision_hash = _hash(
        {"paths": paths, "admissible": admissible, "deficits": deficits}
    )
    return admissible, deficits, decision_hash


def _planning_paths(
    session: Session,
    task: Task,
    account_ids: list[int],
    *,
    target: OperationTarget | None,
    require_send: bool,
) -> list[dict]:
    accounts = _accounts_by_id(session, task, account_ids)
    authorizations = _authorizations_by_account(session, task, account_ids)
    memberships = _memberships_by_account(session, task, account_ids)
    masks = _masks_by_account(session, task, account_ids)
    ready_ids = {
        account.id for account in channel_member_accounts(
            session, task, target, list(accounts.values()), require_send=require_send
        )
    } if target is not None else set()
    return [
        _account_path(
            task,
            account_id,
            accounts.get(account_id),
            authorization=authorizations.get(account_id),
            membership=memberships.get(account_id),
            mask=masks.get(account_id),
            target=target,
            membership_ready=account_id in ready_ids,
        )
        for account_id in account_ids
    ]


def _new_snapshot(
    task: Task,
    participation: TaskParticipationUnitPlan,
    *,
    planning_horizon: str,
    dependency_hash: str,
    paths: list[dict],
    admissible: list[int],
    deficits: list[int],
    valid_until: datetime,
    decision_hash: str,
) -> PlanningAdmissionSnapshot:
    return PlanningAdmissionSnapshot(
        tenant_id=task.tenant_id,
        task_id=task.id,
        task_lifecycle_epoch=task.task_lifecycle_epoch,
        participation_plan_id=participation.id,
        participation_unit=participation.participation_unit,
        planning_horizon=planning_horizon,
        dependency_revision_set_hash=dependency_hash,
        account_paths=paths,
        admissible_account_ids=admissible,
        deficit_account_ids=deficits,
        decision=_decision(len(paths), len(admissible)),
        valid_until=valid_until,
        decision_hash=decision_hash,
    )


def _account_path(
    task: Task,
    account_id: int,
    account: TgAccount | None,
    *,
    authorization: TgAccountAuthorization | None,
    membership: TaskMembershipAdmissionItem | None,
    mask: AiAccountVoiceProfile | None,
    target: OperationTarget | None,
    membership_ready: bool,
) -> dict:
    checks = [
        _account_check(account),
        _session_check(account, authorization),
        _proxy_check(account),
        _membership_check(
            target,
            membership=membership,
            ready=membership_ready,
        ),
        _comment_profile_check(task, account),
        _mask_check(task, mask),
        _runtime_dependency("provider"),
        _runtime_dependency("timeline_and_behavior_session"),
    ]
    blocking = [check["reason"] for check in checks if check["blocking"] and check["status"] != "ready"]
    return {
        "account_id": account_id,
        "admissible": not blocking,
        "blocking_reasons": blocking,
        "dependencies": checks,
    }


def _account_check(account: TgAccount | None) -> dict:
    ready = bool(
        account
        and account.deleted_at is None
        and account.status == AccountStatus.ACTIVE.value
        and account.account_lifecycle_status == "business_active"
    )
    revision = None if account is None else {
        "authorization_generation": account.authorization_generation,
        "authorization_fact_generation": account.authorization_fact_generation,
        "connection_generation": account.connection_generation,
        "status": account.status,
    }
    return _check("account", ready, "account_not_business_active", revision=revision)


def _session_check(
    account: TgAccount | None, authorization: TgAccountAuthorization | None
) -> dict:
    account_session = bool(account and account.session_ciphertext)
    auth_session = bool(
        authorization
        and authorization.status == "active"
        and authorization.session_ciphertext
    )
    revision = None if authorization is None else {
        "authorization_id": authorization.id,
        "slot_generation": authorization.slot_generation,
        "fact_version": authorization.fact_version,
        "health_status": authorization.health_status,
    }
    return _check("session", account_session or auth_session, "session_unavailable", revision=revision)


def _proxy_check(account: TgAccount | None) -> dict:
    if account is None or account.proxy is None:
        return _check("proxy", True, "proxy_not_required", revision={"mode": "direct"})
    proxy = account.proxy
    ready = proxy.status == "healthy" and proxy.alert_status == "normal"
    revision = {
        "proxy_id": proxy.id,
        "status": proxy.status,
        "alert_status": proxy.alert_status,
        "last_check_at": (
            proxy.last_check_at.isoformat() if proxy.last_check_at else None
        ),
    }
    return _check("proxy", ready, "proxy_unhealthy_or_unproven", revision=revision)


def _membership_check(
    target: OperationTarget | None,
    *,
    membership: TaskMembershipAdmissionItem | None,
    ready: bool,
) -> dict:
    if target is None:
        return _check("target_membership", True, "target_not_applicable", revision=None)
    revision = {
        "target_id": target.id,
        "target_reference_revision": target.reference_revision,
        "membership_item_id": membership.id if membership else None,
        "membership_phase": membership.phase if membership else "unobserved",
        "eligibility_revision": membership.eligibility_revision if membership else 0,
    }
    return _check("target_membership", ready, "target_membership_unavailable", revision=revision)


def _mask_check(task: Task, mask: AiAccountVoiceProfile | None) -> dict:
    if task.type not in CONTENT_TASK_TYPES:
        return _check("account_mask", True, "mask_not_required", revision=None)
    ready = bool(mask and mask.status == "active" and mask.quality_status == "active" and mask.short_prompt_summary)
    revision = None if mask is None else {
        "mask_id": mask.id,
        "version": mask.version,
        "status": mask.status,
        "quality_status": mask.quality_status,
    }
    return _check("account_mask", ready, "account_mask_unavailable", revision=revision)


def _comment_profile_check(task: Task, account: TgAccount | None) -> dict:
    if task.type != "channel_comment":
        return _check("comment_public_profile", True, "profile_not_required", revision=None)
    ready = bool(account and comment_account_profile_ready(account))
    revision = None if account is None else {
        "profile_sync_status": account.profile_sync_status,
        "profile_synced_at": (
            account.profile_synced_at.isoformat()
            if account.profile_synced_at
            else None
        ),
        "has_username": bool(account.username),
        "has_avatar": bool(account.avatar_object_key),
    }
    return _check(
        "comment_public_profile",
        ready,
        "comment_public_profile_unavailable",
        revision=revision,
    )


def _runtime_dependency(kind: str) -> dict:
    return {
        "kind": kind,
        "status": "runtime_gate",
        "blocking": False,
        "reason": f"{kind}_requires_runtime_admission",
        "revision": None,
    }


def _check(kind: str, ready: bool, reason: str, *, revision) -> dict:
    return {
        "kind": kind,
        "status": "ready" if ready else "deficit",
        "blocking": True,
        "reason": "" if ready else reason,
        "revision": revision,
    }


def _accounts_by_id(session: Session, task: Task, account_ids: list[int]) -> dict[int, TgAccount]:
    rows = session.scalars(select(TgAccount).options(joinedload(TgAccount.proxy)).where(
        TgAccount.tenant_id == task.tenant_id, TgAccount.id.in_(account_ids)))
    return {row.id: row for row in rows}


def _authorizations_by_account(session: Session, task: Task, account_ids: list[int]) -> dict[int, TgAccountAuthorization]:
    rows = session.scalars(select(TgAccountAuthorization).where(
        TgAccountAuthorization.tenant_id == task.tenant_id,
        TgAccountAuthorization.account_id.in_(account_ids),
        TgAccountAuthorization.is_current.is_(True),
    ))
    return {row.account_id: row for row in rows}


def _memberships_by_account(session: Session, task: Task, account_ids: list[int]) -> dict[int, TaskMembershipAdmissionItem]:
    rows = session.scalars(select(TaskMembershipAdmissionItem).where(
        TaskMembershipAdmissionItem.task_id == task.id,
        TaskMembershipAdmissionItem.account_id.in_(account_ids),
    ))
    return {row.account_id: row for row in rows}


def _masks_by_account(session: Session, task: Task, account_ids: list[int]) -> dict[int, AiAccountVoiceProfile]:
    rows = session.scalars(select(AiAccountVoiceProfile).where(
        AiAccountVoiceProfile.tenant_id == task.tenant_id,
        AiAccountVoiceProfile.account_id.in_(account_ids),
    ).order_by(AiAccountVoiceProfile.account_id, AiAccountVoiceProfile.version.desc()))
    result: dict[int, AiAccountVoiceProfile] = {}
    for row in rows:
        result.setdefault(row.account_id, row)
    return result


def _snapshot(
    session,
    task,
    participation,
    *,
    horizon,
    decision_hash,
    observed_at,
):
    return session.scalar(
        select(PlanningAdmissionSnapshot)
        .where(
            PlanningAdmissionSnapshot.task_id == task.id,
            PlanningAdmissionSnapshot.task_lifecycle_epoch
            == task.task_lifecycle_epoch,
            PlanningAdmissionSnapshot.participation_unit
            == participation.participation_unit,
            PlanningAdmissionSnapshot.planning_horizon == horizon,
            PlanningAdmissionSnapshot.decision_hash == decision_hash,
            PlanningAdmissionSnapshot.valid_until > observed_at,
        )
        .order_by(PlanningAdmissionSnapshot.created_at.desc())
        .limit(1)
    )


def _observation_dependency_hash(paths: list[dict], observed_at: datetime) -> str:
    epoch_seconds = int(OBSERVATION_TTL.total_seconds())
    return _hash(
        {
            "paths": paths,
            "observation_epoch": int(observed_at.timestamp()) // epoch_seconds,
        }
    )


def _decision(required: int, admissible: int) -> str:
    if required == admissible:
        return "achievable"
    return "partially_serviceable" if admissible else "blocked"


def _project_snapshot(task: Task, snapshot: PlanningAdmissionSnapshot) -> None:
    task.stats = {
        **(task.stats or {}),
        "planning_admission_snapshot_id": snapshot.id,
        "planning_admission_decision": snapshot.decision,
        "planning_required_account_count": len(snapshot.account_paths or []),
        "planning_admissible_account_count": len(snapshot.admissible_account_ids or []),
        "planning_deficit_account_count": len(snapshot.deficit_account_ids or []),
    }


def _hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(payload).hexdigest()


__all__ = ["ensure_planning_admission_snapshot"]
