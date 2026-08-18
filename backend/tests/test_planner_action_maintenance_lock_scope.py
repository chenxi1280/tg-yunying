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
    assert "voice_profile_anchor_rewritten" in sql[0]
    assert "coverage_ledger_id" in sql[1]
    assert "hard_hourly_target" in sql[4]


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


def test_current_group_ai_maintenance_skips_obsolete_action_scans() -> None:
    task = SimpleNamespace(
        id="task-ai-current",
        tenant_id=1,
        type="group_ai_chat",
        fulfillment_contract_version=CURRENT_CONTRACT_VERSION,
    )
    session = _CaptureSession()

    retired = fulfillment_takeover_actions.retire_unbound_legacy_actions_for_planner(
        session, task,
    )
    backfilled = group_ai_chat._backfill_open_action_admission_snapshots(
        session, task,
    )

    assert retired == 0
    assert backfilled == 0
    assert session.statements == []


def test_current_daily_prepare_does_not_materialize_account_candidates(
    monkeypatch,
) -> None:
    task = SimpleNamespace(
        id="task-ai-current",
        tenant_id=1,
        type="group_ai_chat",
        account_config={"selection_mode": "all"},
        type_config={"account_coverage_mode": "all_accounts_daily"},
        pacing_config={},
        fulfillment_contract_version=CURRENT_CONTRACT_VERSION,
        stats={},
    )
    session = _CaptureSession()
    monkeypatch.setattr(
        group_ai_chat,
        "expire_legacy_anchor_rewritten_actions",
        lambda *_args: 0,
    )
    monkeypatch.setattr(
        group_ai_chat,
        "expire_incomplete_daily_contract_actions",
        lambda *_args: 0,
    )
    monkeypatch.setattr(
        group_ai_chat,
        "_skip_legacy_hard_hourly_open_actions_for_daily_coverage_replan",
        lambda *_args: 0,
    )
    monkeypatch.setattr(
        group_ai_chat,
        "_select_accounts_for_plan",
        lambda *_args, **_kwargs: pytest.fail(
            "current daily maintenance must not scan accounts"
        ),
    )

    assert group_ai_chat.prepare_open_actions_for_planning(session, task) == 0
