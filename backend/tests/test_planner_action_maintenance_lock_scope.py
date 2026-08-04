from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.services.task_center import fulfillment_takeover_actions
from app.services.task_center import legacy_anchor_rewrite
from app.services.task_center.executors import group_ai_chat
from app.services.task_center.fulfillment_activation import CURRENT_CONTRACT_VERSION


pytestmark = pytest.mark.no_postgres


class _CaptureSession:
    def __init__(self) -> None:
        self.statements = []

    def scalars(self, statement):
        self.statements.append(statement)
        return []


def test_planner_action_maintenance_skips_dispatcher_locked_rows() -> None:
    task = SimpleNamespace(
        id="task-ai",
        tenant_id=1,
        type="group_ai_chat",
        stats={},
    )
    session = _CaptureSession()

    legacy_anchor_rewrite.expire_legacy_anchor_rewritten_actions(session, task)
    legacy_anchor_rewrite.expire_incomplete_daily_contract_actions(session, task)
    fulfillment_takeover_actions.retire_unbound_legacy_actions_for_planner(
        session,
        task,
    )
    group_ai_chat._expire_open_profileless_actions(session, task, [101])
    group_ai_chat._open_hard_hourly_actions_for_distribution_replan(session, task)

    sql = [
        str(statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        ))
        for statement in session.statements
    ]
    assert len(sql) == 5
    assert all("FOR UPDATE OF actions SKIP LOCKED" in statement for statement in sql)
    assert all("'claiming'" not in statement for statement in sql)
    assert all("'executing'" not in statement for statement in sql)


def test_planner_maintenance_keeps_fact_first_group_ai_action_without_legacy_slot() -> None:
    task = SimpleNamespace(
        type="group_ai_chat",
        fulfillment_contract_version=CURRENT_CONTRACT_VERSION,
    )
    action = SimpleNamespace(
        action_type="send_message",
        primary_quantity_slot_id=None,
    )

    assert not fulfillment_takeover_actions._legacy_action_unbound(task, action)
