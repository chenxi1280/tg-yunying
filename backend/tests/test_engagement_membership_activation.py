import pytest
from sqlalchemy import delete, func, select

from app.models import (
    AccountGroupMembershipRevision, AccountGroupMembershipSnapshotSet, AccountGroupStateRevision, AccountPool,
    TaskAccountGroupBindingSetRevision, TgAccount,
)
from app.services.account_group_revisions import begin_membership_change, finish_membership_change
from app.services.task_center.engagement_binding import (
    activate_due_binding, freeze_membership_snapshot, synchronize_task_binding,
)
from app.services.task_center.service import create_channel_like_task, start_task_in_transaction
from tests.test_account_group_revisions import _initialize, _seed
from tests.test_engagement_account_binding import _payload, _seed as _binding_seed
from tests.test_engagement_membership_foundation import _task
from tests.test_engagement_runtime_resources import _session


pytestmark = pytest.mark.no_postgres


def _clear_foundation(session):
    for model in (AccountGroupMembershipRevision, AccountGroupStateRevision):
        session.execute(delete(model))


def _runtime_task(session, status):
    _binding_seed(session)
    task = create_channel_like_task(session, 1, _payload(), "test")
    task.status = status
    session.flush()
    return task


def test_new_binding_rejects_missing_foundation_without_creating_evidence():
    with _session() as session:
        _seed(session)
        with pytest.raises(ValueError, match="account_group_revision_missing"):
            _task(session, status="draft")
        assert session.scalar(select(func.count(TaskAccountGroupBindingSetRevision.id))) == 0
        assert session.scalar(select(func.count(AccountGroupMembershipRevision.id))) == 0
        assert session.scalar(select(func.count(AccountGroupStateRevision.id))) == 0


@pytest.mark.parametrize("status", ["draft", "paused"])
@pytest.mark.parametrize("defect", ["missing", "drift"])
def test_start_and_resume_reject_invalid_foundation_before_runtime_mutation(status, defect):
    with _session() as session:
        task = _runtime_task(session, status)
        if defect == "missing":
            _clear_foundation(session)
        else:
            session.get(TgAccount, 11).pool_id = 2
            session.flush()
        previous_epoch = task.task_lifecycle_epoch
        previous_due = task.next_run_at
        with pytest.raises(ValueError, match=f"account_group_revision_{defect}"):
            start_task_in_transaction(session, task, "test")
        assert task.status == status and task.next_run_at == previous_due
        assert task.task_lifecycle_epoch == previous_epoch


def test_due_binding_rechecks_foundation_before_superseding_current_binding():
    with _session() as session:
        _seed(session)
        _initialize(session)
        task = _task(session)
        current = session.scalar(select(TaskAccountGroupBindingSetRevision))
        task.type_config = {**task.type_config, "account_group_ids": [2]}
        due = synchronize_task_binding(session, task)
        session.flush()
        _clear_foundation(session)
        with pytest.raises(ValueError, match="account_group_revision_missing"):
            activate_due_binding(session, task, period_start=due.effective_from)
        assert current.state == "active" and due.state == "scheduled"
        assert task.type_config["account_group_ids"] == [1]


def test_start_foundation_check_keeps_disabled_active_group_and_saved_snapshot():
    with _session() as session:
        task = _runtime_task(session, "paused")
        snapshot = freeze_membership_snapshot(session, task, participation_unit="original")
        session.flush()
        change = begin_membership_change(session, 1, (1,), actor="test", reason="disabled")
        session.get(AccountPool, 1).is_enabled = False
        finish_membership_change(session, change)
        start_task_in_transaction(session, task, "test")
        assert task.status in {"running", "pending"}
        assert task.type_config["account_group_ids"] == [1, 2]
        assert session.get(AccountGroupMembershipSnapshotSet, snapshot.id) is snapshot
        assert session.scalar(select(func.count(AccountGroupMembershipSnapshotSet.id))) == 1
        assert snapshot.member_account_ids == [11, 21]


def test_new_binding_exposes_wrong_member_purpose_before_creation():
    with _session() as session:
        _seed(session)
        session.get(TgAccount, 11).account_identity = "rank_deboost"
        session.flush()
        _initialize(session)
        with pytest.raises(ValueError, match="account_group_member_purpose_mismatch"):
            _task(session, status="draft")
        assert session.scalar(select(func.count(TaskAccountGroupBindingSetRevision.id))) == 0
