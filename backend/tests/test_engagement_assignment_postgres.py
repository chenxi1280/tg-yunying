"""Actual PostgreSQL ordering of account observations and assignment commits."""
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session
from sqlalchemy.exc import DBAPIError

from app.models import Action, Task, Tenant, TgAccount, TgAccountOnlineState, TgAccountAuthorization
from app.services.task_center.account_assignment_eligibility import assignment_decisions
from app.services.task_center.engagement_runtime_error import RuntimeResourceBlocked
from app.services.task_center.payloads import _create_action, SendMessagePayload
from tests.test_engagement_assignment_eligibility import NOW
from tests.test_runtime_retention_protection_postgres import database

pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]


def _seed(session):
    session.add(Tenant(id=1, name="qualification QA"))
    session.flush()
    session.add_all(TgAccount(id=identity, tenant_id=1, display_name="QA", phone_masked=str(identity),
        status="在线", session_ciphertext="QA") for identity in (11, 12))
    session.flush()
    task = Task(id="qa-task", tenant_id=1, type="channel_view", name="QA", status="running",
        type_config={"engagement_contract_version": "unified_engagement_v1"})
    session.add(task)
    session.flush()
    return task


@pytest.mark.parametrize("kind", ("frozen", "session"))
def test_committed_invalidation_wins_over_stale_orm_assignment(database, kind):
    with Session(database) as seed:
        task = _seed(seed)
        task_id = task.id
        seed.commit()
    with Session(database) as planner:
        stale = planner.get(TgAccount, 11)
        with Session(database) as observer:
            account = observer.get(TgAccount, 11)
            if kind == "frozen":
                account.telegram_frozen = True
            else:
                account.status = "Session失效"
            observer.commit()
        assert stale.status == "在线" and not stale.telegram_frozen
        task = planner.get(Task, task_id)
        with pytest.raises(RuntimeResourceBlocked):
            _create_action(planner, task, "view_message", 11, NOW, SendMessagePayload(group_id=1, message_text="QA"))
        assert planner.scalar(select(Action.id)) is None
        assert _create_action(planner, task, "view_message", 12, NOW, SendMessagePayload(group_id=1, message_text="QA")).account_id == 12


@pytest.mark.parametrize("kind", ("account", "online", "authorization"))
def test_observation_lock_blocks_assignment_without_unknown_or_rows(database, kind):
    with Session(database) as seed:
        _seed(seed)
        seed.add(TgAccountOnlineState(tenant_id=1, account_id=11))
        authorization = TgAccountAuthorization(tenant_id=1, account_id=11, is_current=True,
            status="active", session_ciphertext="QA-current")
        seed.add(authorization)
        seed.flush()
        seed.get(TgAccount, 11).current_authorization_id = authorization.id
        seed.commit()
    with Session(database) as observer, Session(database) as planner:
        model = {"account": TgAccount, "online": TgAccountOnlineState, "authorization": TgAccountAuthorization}[kind]
        column = model.id if kind == "account" else model.account_id
        observer.scalar(select(model).where(column == 11).with_for_update())
        with pytest.raises(RuntimeResourceBlocked) as caught:
            assignment_decisions(planner, 1, [11])
        assert caught.value.code == "account_eligibility_busy"
        assert planner.scalar(select(Action.id)) is None
        assert assignment_decisions(planner, 1, [12]) == {12: ""}


def test_assignment_lock_serializes_following_freeze_observation(database):
    with Session(database) as seed:
        _seed(seed)
        seed.commit()
    with Session(database) as planner, Session(database) as observer:
        assert assignment_decisions(planner, 1, [11]) == {11: ""}
        observer.execute(text("SET LOCAL lock_timeout = '100ms'"))
        with pytest.raises(DBAPIError) as caught:
            observer.execute(update(TgAccount).where(TgAccount.id == 11).values(telegram_frozen=True))
        assert caught.value.orig.sqlstate == "55P03"
        observer.rollback()
        planner.commit()
        observer.get(TgAccount, 11).telegram_frozen = True
        observer.commit()
        assert assignment_decisions(planner, 1, [11]) == {11: "account_frozen"}


def test_concurrent_qualification_readers_share_identity_locks(database):
    with Session(database) as seed:
        _seed(seed)
        seed.commit()
    with Session(database) as first, Session(database) as second:
        assert assignment_decisions(first, 1, [11, 12]) == {11: "", 12: ""}
        assert assignment_decisions(second, 1, [11, 12]) == {11: "", 12: ""}


def test_qualification_read_does_not_block_action_or_attempt_account_foreign_keys(database):
    from app.models import ExecutionAttempt

    with Session(database) as seed:
        _seed(seed)
        seed.commit()
    with Session(database) as reader, Session(database) as writer:
        assert assignment_decisions(reader, 1, [11]) == {11: ""}
        writer.execute(text("SET LOCAL lock_timeout = '100ms'"))
        action = Action(task_id="qa-task", tenant_id=1, task_type="channel_view",
            action_type="view_message", account_id=11)
        writer.add(action)
        writer.flush()
        writer.add(ExecutionAttempt(tenant_id=1, action_id=action.id, account_id=11))
        writer.commit()
        assert reader.scalar(select(Action.id)) == action.id
