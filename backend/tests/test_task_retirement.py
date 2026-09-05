from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import Action, ExecutionAttempt, Task, TaskAccountGroupBindingSetRevision
from app.schemas import ChannelLikeTaskConfigUpdate, TaskRetryRequest, TaskSettingsUpdate, TaskUpdate
from app.services._common import _now
from app.services.task_center import dispatcher, service
from app.services.task_center.engagement_binding import freeze_membership_snapshot
from app.services.task_center.task_retirement import TaskGatewayFenced, lock_task_for_planning
from tests.test_engagement_account_binding import _payload, _seed, _session


pytestmark = pytest.mark.no_postgres


def _tasks(session):
    _seed(session)
    old = service.create_channel_like_task(session, 1, _payload(), "test")
    replacement = service.create_channel_like_task(session, 1, _payload(name="新任务"), "test")
    old.status = "stopped"
    old.next_run_at = None
    old.retired_at = _now()
    old.replaced_by_task_id = replacement.id
    old.task_lifecycle_epoch += 1
    session.commit()
    return old, replacement


@pytest.mark.parametrize("operation", ["start", "reset", "retry", "update", "settings", "type_config"])
def test_retired_task_rejects_operator_reactivation_and_keeps_history(operation):
    with _session() as session:
        old, replacement = _tasks(session)
        previous = (old.task_lifecycle_epoch, old.type_config, old.stats)
        with pytest.raises(ValueError, match="task_retired"):
            if operation == "start":
                service.start_task_in_transaction(session, old, "test")
            elif operation == "reset":
                service.reset_task(session, 1, old.id, "test")
            elif operation == "retry":
                service.retry_task(session, 1, old.id, TaskRetryRequest(), "test")
            elif operation == "settings":
                service.update_task_settings(session, 1, old.id, TaskSettingsUpdate(name="覆盖"), "test")
            elif operation == "type_config":
                service.update_channel_like_config(session, 1, old.id,
                    ChannelLikeTaskConfigUpdate(target_channel_id=101, target_likes_per_message=42), "test")
            else:
                service.update_task(session, 1, old.id, TaskUpdate(name="覆盖"), "test")
        assert previous == (old.task_lifecycle_epoch, old.type_config, old.stats)
        assert old.replaced_by_task_id == replacement.id
        assert lock_task_for_planning(session, old.id) is None


@pytest.mark.parametrize("changes", [{"status": "running"}, {"next_run_at": _now()},
    {"replaced_by_task_id": None}, {"retired_at": None}])
def test_database_rejects_partial_or_nonterminal_retirement(changes):
    with _session() as session:
        old, _ = _tasks(session)
        with pytest.raises(IntegrityError), session.begin_nested():
            for key, value in changes.items():
                setattr(old, key, value)
            session.flush()
        assert session.get(Task, old.id).status == "stopped"


def test_first_start_binding_matches_runtime_epoch_and_can_freeze_members():
    with _session() as session:
        _seed(session)
        task = service.create_channel_like_task(session, 1, _payload(), "test")
        initial = session.scalar(select(TaskAccountGroupBindingSetRevision)
            .where(TaskAccountGroupBindingSetRevision.task_id == task.id))
        service.start_task_in_transaction(session, task, "test")
        session.flush()
        active = session.scalar(select(TaskAccountGroupBindingSetRevision).where(
            TaskAccountGroupBindingSetRevision.task_id == task.id,
            TaskAccountGroupBindingSetRevision.state == "active"))
        snapshot = freeze_membership_snapshot(session, task, participation_unit="first-start")
        assert initial.state == "superseded" and active.supersedes_revision_id == initial.id
        assert active.task_lifecycle_epoch == task.task_lifecycle_epoch
        assert active.binding_set_hash == initial.binding_set_hash
        assert snapshot.member_account_ids == [11, 21]


@pytest.mark.parametrize("task_type", ["group_ai_chat", "channel_comment", "channel_like", "channel_view"])
def test_retired_gateway_attempt_never_reaches_resource_or_call_issued(monkeypatch, task_type):
    with _session() as session:
        old, _ = _tasks(session)
        action = Action(tenant_id=1, task_id=old.id, task_type=task_type,
            action_type="send_message", status="executing", task_lifecycle_epoch=1)
        session.add(action)
        session.flush()
        attempt = ExecutionAttempt(tenant_id=1, action_id=action.id, account_id=11,
            status="before_call", task_lifecycle_epoch=1)
        session.add(attempt)
        session.flush()
        monkeypatch.setattr(dispatcher, "mark_engagement_attempt_call_issued",
            lambda *_a, **_kw: pytest.fail("call issuance passed a retired task"))
        with pytest.raises(TaskGatewayFenced):
            dispatcher._mark_gateway_call_started(session, attempt)
        assert attempt.gateway_call_started_at is None
        assert attempt.status == "skipped_before_gateway"
        assert attempt.after_call_at is not None


def test_scheduled_first_start_uses_pending_epoch_binding():
    with _session() as session:
        _seed(session)
        task = service.create_channel_like_task(session, 1,
            _payload(scheduled_start=_now() + timedelta(hours=1)), "test")
        service.start_task_in_transaction(session, task, "test")
        session.flush()
        binding = session.scalar(select(TaskAccountGroupBindingSetRevision).where(
            TaskAccountGroupBindingSetRevision.task_id == task.id,
            TaskAccountGroupBindingSetRevision.state == "active"))
        assert task.status == "pending"
        assert binding.task_lifecycle_epoch == task.task_lifecycle_epoch


def test_retirement_does_not_reclassify_unknown_when_legacy_call_time_is_missing():
    with _session() as session:
        old, _ = _tasks(session)
        action = Action(tenant_id=1, task_id=old.id, task_type=old.type,
            action_type="like_message", status="unknown_after_send")
        session.add(action)
        session.flush()
        attempt = ExecutionAttempt(tenant_id=1, action_id=action.id, status="result_unknown",
            result_snapshot={"remote_mutation_started": None})
        session.add(attempt)
        session.flush()
        with pytest.raises(RuntimeError, match="attempt_already_called"):
            dispatcher._mark_gateway_call_started(session, attempt)
        assert attempt.status == "result_unknown"
        assert attempt.result_snapshot == {"remote_mutation_started": None}
