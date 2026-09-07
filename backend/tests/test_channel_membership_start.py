from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AccountPool, Action, ExecutionAttempt, OperationTarget, Task, Tenant, TgAccount
from app.schemas.task_center import ChannelCommentTaskCreate, ChannelLikeTaskCreate, ChannelViewTaskCreate
from app.services._common import _now
from app.services.task_center import dispatcher, service
from app.services.task_center.channel_membership import gate_channel_membership, mark_channel_membership_joined
from app.services.task_center.direct_action_claims import claim_fact_first_candidates
from app.services.task_center.task_creation_contract import execute_task_creation_contract
from app.services.task_center.task_retirement import guard_attempt_call_start, TaskGatewayFenced


pytestmark = pytest.mark.no_postgres
MODELS = {
    "channel_view": ChannelViewTaskCreate,
    "channel_like": ChannelLikeTaskCreate,
    "channel_comment": ChannelCommentTaskCreate,
}
ACCOUNT_IDS = frozenset({11, 12, 21, 22})
CHANNEL_ID = 101


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        current.add(Tenant(id=1, name="创建关注"))
        for pool_id in (1, 2, 3):
            current.add(AccountPool(id=pool_id, tenant_id=1, name=str(pool_id), is_default=pool_id == 1))
            for offset in (1, 2):
                account_id = pool_id * 10 + offset
                current.add(TgAccount(
                    id=account_id, tenant_id=1, pool_id=pool_id, display_name=str(account_id),
                    phone_masked=str(account_id), status="在线", session_ciphertext="test-session",
                ))
        current.add(OperationTarget(
            id=CHANNEL_ID, tenant_id=1, target_type="channel", tg_peer_id="-100101", title="测试频道",
        ))
        current.commit()
        yield current
    engine.dispose()


def _create(session, task_type, *, start=True, scheduled_start=None):
    payload = MODELS[task_type](
        name=task_type, target_channel_id=CHANNEL_ID, scheduled_start=scheduled_start,
        client_request_id=f"membership-start-{task_type}",
        account_config={"selection_mode": "group", "account_group_ids": [1, 2], "max_concurrent": 1},
    )
    return execute_task_creation_contract(
        session, tenant_id=1, user_id=7, actor="test", task_type=task_type,
        payload=payload, start_requested=start,
    )


def _actions(session, task):
    return list(session.scalars(select(Action).where(Action.task_id == task.id).order_by(Action.scheduled_at)))


@pytest.mark.parametrize("task_type", MODELS)
def test_create_and_start_materializes_every_account_without_posts_or_planner(session, task_type):
    created = _create(session, task_type)
    assert created.start.start_status == "started"
    rows = _actions(session, created.task)
    assert {row.account_id for row in rows} == ACCOUNT_IDS
    assert {row.action_type for row in rows} == {"ensure_target_membership"}
    assert all(row.status == "pending" for row in rows)
    assert all(row.task_lifecycle_epoch == created.task.task_lifecycle_epoch for row in rows)
    assert timedelta(hours=10) <= rows[-1].scheduled_at - rows[0].scheduled_at <= timedelta(hours=24)
    assert session.scalar(select(ExecutionAttempt)) is None


@pytest.mark.parametrize("task_type", MODELS)
def test_draft_has_no_actions_but_explicit_start_materializes_memberships(session, task_type):
    created = _create(session, task_type, start=False)
    assert created.task.status == "draft"
    assert _actions(session, created.task) == []
    service.start_task(session, 1, created.task.id, "test")
    assert {row.account_id for row in _actions(session, created.task)} == ACCOUNT_IDS


def test_creation_replay_and_planner_keep_the_same_frozen_schedule(session):
    created = _create(session, "channel_view")
    before = [(row.id, row.scheduled_at) for row in _actions(session, created.task)]
    assert len(before) == len(ACCOUNT_IDS)
    replay = _create(session, "channel_view")
    gate_channel_membership(session, replay.task, session.get(OperationTarget, CHANNEL_ID))
    assert [(row.id, row.scheduled_at) for row in _actions(session, replay.task)] == before


def test_joined_accounts_have_audit_rows_instead_of_remote_join_actions(session):
    mark_channel_membership_joined(session, 1, CHANNEL_ID, 11)
    session.commit()
    created = _create(session, "channel_like")
    rows = {row.account_id: row for row in _actions(session, created.task)}
    assert set(rows) == ACCOUNT_IDS
    assert rows[11].status == "skipped"
    assert rows[11].result["membership_status"] == "already_joined"
    assert all(rows[account_id].status == "pending" for account_id in ACCOUNT_IDS - {11})


def test_scheduled_task_can_claim_only_membership_before_main_start(session):
    created = _create(session, "channel_view", scheduled_start=_now() + timedelta(days=2))
    assert created.task.status == "pending"
    rows = _actions(session, created.task)
    assert len(rows) == len(ACCOUNT_IDS)
    due = rows[0]
    batch = claim_fact_first_candidates(
        session, owner="test", limit=10, now=_now(), lease_seconds=60,
    )
    assert batch.action_ids == (due.id,)
    assert dispatcher._confirm_claim(session, due.id, owner=batch.owner, token=batch.token)
    assert dispatcher._fulfillment_route_allows_gateway(session, due)
    attempt = ExecutionAttempt(
        tenant_id=1, action_id=due.id, account_id=due.account_id, attempt_no=1,
        task_lifecycle_epoch=created.task.task_lifecycle_epoch, status="before_call", before_call_at=_now(),
    )
    session.add(attempt)
    session.flush()
    guard_attempt_call_start(session, attempt)
    main_action = Action(
        id="main-before-start", tenant_id=1, task_id=created.task.id, task_type=created.task.type,
        action_type="view_message", account_id=11, status="pending", scheduled_at=_now(),
        task_lifecycle_epoch=created.task.task_lifecycle_epoch,
    )
    session.add(main_action)
    session.flush()
    assert main_action not in dispatcher.due_actions(session)
    assert not dispatcher._fulfillment_route_allows_gateway(session, main_action)


@pytest.mark.parametrize("state", ["draft", "paused", "stopped", "old_epoch", "deleted", "missing_start_marker", "wrong_target"])
def test_start_membership_never_bypasses_lifecycle_fences(session, state):
    created = _create(session, "channel_view", scheduled_start=_now() + timedelta(days=2))
    task = created.task
    action = _actions(session, task)[0]
    if state == "old_epoch":
        task.task_lifecycle_epoch += 1
    elif state == "deleted":
        task.deleted_at = _now()
    elif state == "missing_start_marker":
        task.stats = {key: value for key, value in task.stats.items() if key != "channel_membership_start_epoch"}
    elif state == "wrong_target":
        action.payload = {**action.payload, "channel_target_id": CHANNEL_ID + 1}
    else:
        task.status = state
    session.commit()
    assert not dispatcher._fulfillment_route_allows_gateway(session, action)
    attempt = ExecutionAttempt(
        tenant_id=1, action_id=action.id, account_id=action.account_id, attempt_no=1,
        task_lifecycle_epoch=action.task_lifecycle_epoch, status="before_call", before_call_at=_now(),
    )
    session.add(attempt)
    session.flush()
    with pytest.raises(TaskGatewayFenced):
        guard_attempt_call_start(session, attempt)


def test_start_failure_rolls_back_memberships_but_keeps_created_draft(session, monkeypatch):
    def fail_after_memberships(*args):
        assert session.scalar(select(Action)) is not None
        raise ValueError("injected_start_failure_after_memberships")

    monkeypatch.setattr(service, "_set_runtime_projection", fail_after_memberships)
    created = _create(session, "channel_like")
    assert created.create.create_status == "created"
    assert created.start.start_status == "start_failed"
    assert session.get(Task, created.task.id).status == "draft"
    assert _actions(session, created.task) == []


def test_resume_rebinds_only_uncalled_memberships_without_randomizing_again(session):
    created = _create(session, "channel_view")
    rows = _actions(session, created.task)
    intervals = [right.scheduled_at - left.scheduled_at for left, right in zip(rows, rows[1:])]
    original_ids = {row.id for row in rows}
    service.pause_task(session, 1, created.task.id, "test")
    resumed = service.resume_task(session, 1, created.task.id, "test")
    rows = _actions(session, resumed)
    assert {row.id for row in rows} == original_ids
    assert all(row.task_lifecycle_epoch == resumed.task_lifecycle_epoch for row in rows)
    assert [right.scheduled_at - left.scheduled_at for left, right in zip(rows, rows[1:])] == intervals


@pytest.mark.parametrize("attempt_state", ["before_call", "gateway_call_started", "result_unknown"])
def test_resume_never_rebinds_membership_with_attempt_evidence(session, attempt_state):
    created = _create(session, "channel_view")
    action = _actions(session, created.task)[0]
    original_epoch = action.task_lifecycle_epoch
    original_time = action.scheduled_at
    session.add(ExecutionAttempt(
        tenant_id=1, action_id=action.id, account_id=action.account_id, attempt_no=1,
        task_lifecycle_epoch=original_epoch, status=attempt_state, before_call_at=_now(),
        gateway_call_started_at=None if attempt_state == "before_call" else _now(),
    ))
    session.commit()
    service.pause_task(session, 1, created.task.id, "test")
    resumed = service.resume_task(session, 1, created.task.id, "test")
    rows = _actions(session, resumed)
    assert len(rows) == len(ACCOUNT_IDS)
    assert action.task_lifecycle_epoch == original_epoch
    assert action.scheduled_at == original_time
    assert all(row.task_lifecycle_epoch == resumed.task_lifecycle_epoch for row in rows if row.id != action.id)
