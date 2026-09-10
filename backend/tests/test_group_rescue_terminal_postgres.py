"""A cached rescue cannot overwrite a concurrently committed terminal state."""
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
import pytest

from app.models import Action, Task, Tenant, TgGroup
from app.services.task_center.group_rescue import refresh_group_rescue_action
from app.services.task_center.ai_generation_outcome_diagnostics import generation_outcome_diagnostics
from tests.postgres_pacing_e4_fixture import factory as factory

pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]


def _seed(factory):
    with factory() as session:
        session.add(Tenant(id=1, name="neutral"))
        session.flush()
        session.add_all([Task(id="task", tenant_id=1, name="neutral", type="group_ai_chat"),
            TgGroup(id=7, tenant_id=1, title="neutral", tg_peer_id="-1007")])
        session.flush()
        session.add(Action(id="rescue", tenant_id=1, task_id="task", task_type="group_ai_chat",
            action_type="invite_group_account", status="skipped", result={"rescue_status": "pending"}))
        session.commit()


def _refresh(session, action):
    return refresh_group_rescue_action(session, session.get(Task, "task"), session.get(TgGroup, 7), action,
        trigger_account_id=11, trigger_reason="configuration refresh", operation_target_id=None)


def test_stale_dirty_object_does_not_flush_over_new_terminal_state(factory):
    _seed(factory)
    with factory() as reader, factory() as writer:
        stale = reader.get(Action, "rescue")
        latest = writer.get(Action, "rescue")
        latest.status = "closed_unknown"
        latest.result = {"rescue_status": "pending", "original_evidence": "retained"}
        writer.commit()
        stale.status = "pending"
        outcome = _refresh(reader, stale)
        assert outcome.status == "closed_unknown"
        assert stale.status == "closed_unknown"
        reader.commit()
    with factory() as check:
        action = check.get(Action, "rescue")
        assert action.status == "closed_unknown"
        assert action.result["original_evidence"] == "retained"


def test_refresh_lock_contention_cannot_change_the_action(factory):
    _seed(factory)
    with factory() as writer, factory() as reader:
        writer.scalar(select(Action).where(Action.id == "rescue").with_for_update())
        with pytest.raises(OperationalError, match="lock timeout"):
            _refresh(reader, reader.get(Action, "rescue"))
        reader.rollback()
        writer.rollback()
        assert reader.get(Action, "rescue").status == "skipped"


def test_outcome_diagnostics_executes_correlated_query_on_postgres(factory):
    _seed(factory)
    with factory() as session:
        session.add(Action(id="send", tenant_id=1, task_id="task", task_type="group_ai_chat",
            action_type="send_message", status="success", payload={}))
        session.commit()
        result = generation_outcome_diagnostics(session, session.get(Task, "task"))
        assert result["outcome_counts"] == {"receipt_without_visible_fact": 1}
