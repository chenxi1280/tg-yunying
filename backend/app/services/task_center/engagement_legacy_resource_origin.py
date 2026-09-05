"""Resolve explicitly enrolled legacy work without replacing its original plan."""
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountGroupMembershipSnapshotSet, Action, TaskDayLedger
from app.services._common import _now
from app.timezone import as_beijing

from .engagement_account_origin import FrozenAccountOrigin
from .engagement_action_contract import first_engagement_binding
from .engagement_binding import ENGAGEMENT_TASK_TYPES, _stable_hash


INITIAL_CUTOVER_SNAPSHOT_REVISION = 1
SNAPSHOT_AMBIGUITY_LIMIT = 2


def legacy_cutover_origin(session: Session, action: Action) -> FrozenAccountOrigin | None:
    if action.task_type not in ENGAGEMENT_TASK_TYPES:
        return None
    binding = first_engagement_binding(session, action)
    if binding is None or binding.state == "scheduled":
        return None
    snapshots = list(session.scalars(select(AccountGroupMembershipSnapshotSet).where(
        AccountGroupMembershipSnapshotSet.task_id == action.task_id,
        AccountGroupMembershipSnapshotSet.participation_unit == f"legacy_cutover:{binding.id}",
    ).limit(SNAPSHOT_AMBIGUITY_LIMIT)))
    if not snapshots:
        return None
    if len(snapshots) != 1:
        raise ValueError("legacy_cutover_snapshot_ambiguous")
    snapshot = snapshots[0]
    _validate_snapshot(action, binding, snapshot=snapshot)
    pool_id = _account_origin(action, binding, snapshot=snapshot)
    return FrozenAccountOrigin(pool_id, binding, "legacy_cutover_snapshot",
        membership_snapshot_set_id=snapshot.id)


def _validate_snapshot(action, binding, *, snapshot):
    if (snapshot.tenant_id, snapshot.task_id, snapshot.task_lifecycle_epoch) != (
            action.tenant_id, action.task_id, action.task_lifecycle_epoch):
        raise ValueError("legacy_cutover_snapshot_owner_mismatch")
    if snapshot.binding_set_revision_id != binding.id:
        raise ValueError("legacy_cutover_snapshot_binding_mismatch")
    if (snapshot.state != "frozen"
            or snapshot.snapshot_set_revision != INITIAL_CUTOVER_SNAPSHOT_REVISION
            or binding.state not in {"active", "superseded"}
            or as_beijing(binding.effective_from) > as_beijing(_now())):
        raise ValueError("legacy_cutover_snapshot_not_effective")
    expected = _stable_hash({"groups": snapshot.group_memberships,
        "members": snapshot.member_account_ids, "origins": snapshot.account_origin_groups})
    if expected != snapshot.member_union_hash:
        raise ValueError("legacy_cutover_snapshot_hash_mismatch")


def _account_origin(action, binding, *, snapshot):
    if action.account_id not in snapshot.member_account_ids:
        raise ValueError("legacy_cutover_account_not_frozen")
    pool_id = int(snapshot.account_origin_groups.get(str(action.account_id)) or 0)
    groups = [group for group in snapshot.group_memberships
        if action.account_id in group.get("member_account_ids", [])]
    if (pool_id not in binding.account_group_ids or len(groups) != 1
            or groups[0].get("group_id") != pool_id):
        raise ValueError("legacy_cutover_account_origin_invalid")
    return pool_id


def assert_legacy_attempt_uncalled(action, attempt):
    if (attempt.tenant_id, attempt.action_id, attempt.account_id,
            attempt.task_lifecycle_epoch) != (
            action.tenant_id, action.id, action.account_id, action.task_lifecycle_epoch):
        raise ValueError("legacy_resource_attempt_owner_mismatch")
    if (attempt.status != "before_call" or attempt.gateway_call_started_at is not None
            or attempt.after_call_at is not None):
        raise ValueError("legacy_resource_requires_uncalled_attempt")


def legacy_original_task_day(session: Session, action: Action) -> date:
    ledger_id = str((action.payload or {}).get("task_day_ledger_id") or "")
    if ledger_id:
        ledger = session.get(TaskDayLedger, ledger_id)
        if ledger is None or (ledger.tenant_id, ledger.task_id) != (
                action.tenant_id, action.task_id):
            raise ValueError("legacy_original_task_day_ledger_invalid")
        return ledger.obligation_local_date
    if action.pacing_due_at is None:
        raise ValueError("legacy_original_task_day_unproven")
    return as_beijing(action.pacing_due_at).date()
