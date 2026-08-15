from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import Action, FulfillmentRemoteFact
from app.services._common import _now
from app.services.task_center.pacing_summary import task_pacing_summary


pytestmark = pytest.mark.no_postgres


def _session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Action.__table__.create(engine)
    FulfillmentRemoteFact.__table__.create(engine)
    return Session(engine)


def _task(contract: str = "fact_first_v3"):
    return SimpleNamespace(tenant_id=1, id="task-1", fulfillment_contract_version=contract)


def _action(
    index: int,
    *,
    due_offset_seconds: float,
    status: str,
    scheduled_at=None,
    executed_at=None,
    account_id: int = 11,
) -> Action:
    due = _now() + timedelta(seconds=due_offset_seconds)
    return Action(
        id=f"action-{index}",
        tenant_id=1,
        task_id="task-1",
        task_type="channel_view",
        action_type="view_message",
        account_id=account_id,
        status=status,
        scheduled_at=scheduled_at if scheduled_at is not None else due,
        executed_at=executed_at,
        pacing_due_at=due,
        pacing_slot_key=f"slot-{index}",
    )


def _confirmed_fact(action: Action, index: int, *, observed_at=None) -> FulfillmentRemoteFact:
    return FulfillmentRemoteFact(
        fact_id=f"fact-{index}",
        tenant_id=action.tenant_id,
        task_type=action.task_type,
        task_id=action.task_id,
        obligation_type="view",
        obligation_id=f"obligation-{index}",
        action_id=action.id,
        attempt_id=f"attempt-{index}",
        mutation_kind=action.action_type,
        remote_mutation_key_hash=f"mutation-{index}",
        gateway_request_hash=f"request-{index}",
        fact_kind="view_observed",
        fact_identity_hash=f"identity-{index}",
        observed_at=observed_at or action.executed_at or _now(),
    )


def test_summary_counts_states_and_bounds() -> None:
    now = _now()
    with _session() as session:
        actions = [
            _action(1, due_offset_seconds=3600, status="pending"),                      # future
            _action(2, due_offset_seconds=-60, status="pending"),                        # due
            _action(3, due_offset_seconds=-3600, status="executing"),                    # late
            _action(4, due_offset_seconds=-7200, status="success", executed_at=now),     # confirmed
            _action(5, due_offset_seconds=-10800, status="failed"),                      # missed
            _action(6, due_offset_seconds=-12000, status="success", executed_at=now),    # remote_unknown
            _action(7, due_offset_seconds=-13000, status="unknown_after_send"),          # remote_unknown
        ]
        session.add_all(actions)
        session.add(_confirmed_fact(actions[3], 4, observed_at=now))
        session.commit()

        summary = task_pacing_summary(session, _task())

    assert summary["slot_count"] == 7
    assert summary["future"] == 1
    assert summary["due"] == 1
    assert summary["late"] == 1
    assert summary["confirmed"] == 1
    assert summary["remote_unknown"] == 2
    assert summary["missed"] == 1
    assert summary["same_second_count"] == 0
    assert summary["future_to_now_rewrite_count"] == 0
    assert summary["pacing_contract_version"] == "deterministic_stratified_v1"


def test_summary_flags_same_second_and_rewrite_violations() -> None:
    same_second = _now().replace(microsecond=0)
    with _session() as session:
        actions = [
            Action(id="a-1", tenant_id=1, task_id="task-1", task_type="channel_view", action_type="view_message",
                   account_id=11, status="pending", scheduled_at=same_second, pacing_due_at=same_second),
            Action(id="a-2", tenant_id=1, task_id="task-1", task_type="channel_view", action_type="view_message",
                   account_id=12, status="pending", scheduled_at=same_second, pacing_due_at=same_second),
            # scheduled_at 被拉到 due 之前：future→now 违约必须可见
            Action(id="a-3", tenant_id=1, task_id="task-1", task_type="channel_view", action_type="view_message",
                   account_id=13, status="pending", scheduled_at=same_second - timedelta(minutes=5),
                   pacing_due_at=same_second),
        ]
        session.add_all(actions)
        session.add_all([
            _confirmed_fact(action, index, observed_at=action.executed_at)
            for index, action in enumerate(actions, start=1)
        ])
        session.commit()

        summary = task_pacing_summary(session, _task())

    # 3 个 due 落在同一秒 → 重复计数 2
    assert summary["same_second_count"] == 2
    assert summary["five_minute_peak"]["count"] == 3
    assert summary["due_at_unique_ratio"] < 1.0
    assert summary["future_to_now_rewrite_count"] == 1


def test_summary_five_minute_peak_and_account_gaps() -> None:
    base = _now().replace(microsecond=0)
    with _session() as session:
        actions = [
            # 4 个 due 落在 300 秒跨度内：[-300,-100) 窗口覆盖 3 个点 → 峰值 3；
            # 上界 = min(4, ceil(4*300/300)+1) = 4；同账号相邻执行间隔最小 100 秒
            _action(1, due_offset_seconds=-400, status="success", executed_at=base - timedelta(seconds=400)),
            _action(2, due_offset_seconds=-300, status="success", executed_at=base - timedelta(seconds=300)),
            _action(3, due_offset_seconds=-200, status="success", executed_at=base - timedelta(seconds=200)),
            _action(4, due_offset_seconds=-100, status="success", executed_at=base - timedelta(seconds=100)),
        ]
        session.add_all(actions)
        session.add_all([
            _confirmed_fact(action, index, observed_at=action.executed_at)
            for index, action in enumerate(actions, start=1)
        ])
        session.commit()

        summary = task_pacing_summary(session, _task())

    assert summary["five_minute_peak"]["count"] == 3
    assert summary["five_minute_peak"]["upper_bound"] >= 3
    assert summary["account_min_executed_gap_seconds"] == 100.0


def test_summary_empty_for_legacy_tasks_and_missing_due() -> None:
    with _session() as session:
        session.add(Action(id="a-1", tenant_id=1, task_id="task-1", task_type="channel_view",
                           action_type="view_message", account_id=11, status="pending",
                           scheduled_at=_now()))
        session.commit()

        assert task_pacing_summary(session, _task("legacy_v1")) == {}
        assert task_pacing_summary(session, _task()) == {}


def test_action_payload_projects_pacing_fields() -> None:
    from app.services.task_center import service

    with _session() as session:
        session.add(_action(1, due_offset_seconds=600, status="pending"))
        session.commit()

        # 单条 Action 投影冻结 due/slot（§4.7 单条执行详情）
        payload = service._action_payload(session.get(Action, "action-1"))
        assert payload["pacing_due_at"] is not None
        assert payload["effective_claim_at"] is None
        assert payload["release_not_before_at"] is None
        assert payload["pacing_slot_key"] == "slot-1"

        from app.schemas.task_center import ActionOut
        projected = ActionOut.model_validate(dict(payload))
        assert projected.pacing_due_at is not None
        assert projected.effective_claim_at is None
        assert projected.pacing_slot_key == "slot-1"
