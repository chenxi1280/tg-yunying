from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models import (
    Action,
    AccountStatus,
    GroupAuthStatus,
    OperationTarget,
    Task,
    TaskAccountDailyCoverage,
    TaskMembershipAdmissionItem,
    Tenant,
    TgAccount,
    TgGroup,
)
from app.services._common import _now
from app.services.task_center.ai_group_rescue_protected_recovery import (
    BindingEvidence,
    MembershipObservation,
    RecoveryScope,
)


NOW = datetime(2026, 8, 28, 12, 0)


def _seed_base(session: Session) -> None:
    _seed_tenant_and_accounts(session)
    _seed_task_target(session)


def _seed_tenant_and_accounts(session: Session) -> None:
    session.add(Tenant(
        id=1,
        name="tenant",
        group_rescue_enabled=True,
        group_rescue_admin_account_id=101,
    ))
    session.add_all([
        TgAccount(
            id=101,
            tenant_id=1,
            display_name="admin",
            phone_masked="101",
            username="admin",
            session_ciphertext="session-admin",
            status=AccountStatus.ACTIVE.value,
        ),
        TgAccount(
            id=102,
            tenant_id=1,
            display_name="member",
            phone_masked="102",
            username="member",
            session_ciphertext="session-member",
            status=AccountStatus.ACTIVE.value,
        ),
    ])


def _seed_task_target(session: Session) -> None:
    session.add(OperationTarget(
        id=21,
        tenant_id=1,
        target_type="group",
        tg_peer_id="-10011",
        title="same title",
        can_send=True,
        auth_status=GroupAuthStatus.AUTHORIZED.value,
    ))
    session.add(TgGroup(
        id=11,
        tenant_id=1,
        tg_peer_id="-10011",
        title="same title",
        can_send=True,
        auth_status=GroupAuthStatus.AUTHORIZED.value,
    ))
    session.add(Task(
        id="task-1",
        tenant_id=1,
        name="AI group",
        type="group_ai_chat",
        status="running",
        task_lifecycle_epoch=7,
        type_config={
            "target_operation_target_id": 21,
            "target_group_id": 11,
        },
    ))


def _seed_recovery_accounts(session: Session) -> None:
    session.add_all([
        TgAccount(
            id=103,
            tenant_id=1,
            display_name="target admin",
            phone_masked="103",
            username="target_admin",
            session_ciphertext="session-target-admin",
            status=AccountStatus.ACTIVE.value,
        ),
        TgAccount(
            id=104,
            tenant_id=1,
            display_name="target listener",
            phone_masked="104",
            username="target_listener",
            session_ciphertext="session-target-listener",
            status=AccountStatus.ACTIVE.value,
        ),
    ])


def _recovery_scope(*, config_revision: int) -> RecoveryScope:
    return RecoveryScope(
        task_id="task-1",
        expected_epoch=7,
        expected_config_revision=config_revision,
        expected_target_id=21,
        expected_group_id=11,
        deployed_sha="a" * 40,
    )


def _binding_evidence() -> BindingEvidence:
    return BindingEvidence(
        rescue_admin_account_id=103,
        listener_account_id=104,
        target_peer_digest="peer-digest",
        rescue_identity_digest="admin-digest",
        listener_identity_digest="listener-digest",
        rescue_is_admin=True,
        invite_users=True,
        ban_users=True,
        delete_messages=True,
        listener_is_member=True,
        listener_history_readable=True,
    )


def _seed_unknown_rescue(
    session: Session,
) -> tuple[Action, TaskMembershipAdmissionItem, TaskAccountDailyCoverage]:
    old = Action(
        id="old-unknown-rescue",
        tenant_id=1,
        task_id="task-1",
        task_type="group_ai_chat",
        action_type="invite_group_account",
        account_id=101,
        scheduled_at=NOW,
        status="closed_unknown",
        task_lifecycle_epoch=7,
        payload={
            "group_id": 11,
            "operation_target_id": 21,
            "group_peer_id": "-10011",
            "target_account_id": 102,
            "target_account_ref": "@member",
            "trigger_account_id": 102,
            "trigger_task_id": "task-1",
            "trigger_reason": "permission denied",
        },
        result={"rescue_status": "unknown_after_send"},
    )
    item = TaskMembershipAdmissionItem(
        tenant_id=1,
        task_id="task-1",
        account_id=102,
        target_id=21,
        phase="failed",
        rescue_action_id=old.id,
        rescue_status="unknown_after_send",
    )
    session.add_all([old, item])
    session.flush()
    coverage = TaskAccountDailyCoverage(
        tenant_id=1,
        task_id="task-1",
        group_id=11,
        account_id=102,
        membership_item_id=item.id,
        coverage_date=_now().date(),
        state="blocked",
        blocker_code="membership_unknown",
    )
    session.add(coverage)
    session.flush()
    return old, item, coverage


def _membership_observation(outcome: str) -> MembershipObservation:
    return MembershipObservation(
        source_action_id="old-unknown-rescue",
        target_account_id=102,
        outcome=outcome,
        evidence_fingerprint=f"evidence-{outcome}",
    )
