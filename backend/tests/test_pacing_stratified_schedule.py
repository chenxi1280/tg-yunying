from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, Task, TgAccount
from app.services._common import _now
from app.services.task_center import pacing
from app.services.task_center.account_pacing_guard import (
    ACCOUNT_SOFT_PACING_POLICY_VERSION,
    account_policy_not_before,
    effective_claim_at,
)
from app.services.task_center.pacing import schedule_due_times


pytestmark = pytest.mark.no_postgres


def _curve(weights: dict[int, int]) -> dict:
    return {
        "operation_profile": {
            "hourly_activity_curve": [weights.get(hour, 0) for hour in range(24)],
        },
    }


START = datetime(2026, 8, 15, 10, 0)
DEADLINE = datetime(2026, 8, 15, 12, 0)
SEED = "quota-test"


def test_stratified_schedule_conserves_quota() -> None:
    planned = schedule_due_times(
        30,
        _curve({10: 2, 11: 1}),
        start_at=START,
        deadline_at=DEADLINE,
        seed_id=SEED,
    )

    # 守恒：分层方案下小时计划数之和等于 total，无静默丢弃
    assert len(planned) == 30
    assert all(START <= value < DEADLINE for value in planned)


def test_stratified_schedule_is_deterministic_for_stable_slot_keys() -> None:
    keys = [f"comment:obligation-{index}" for index in range(20)]
    kwargs = dict(
        start_at=START,
        deadline_at=DEADLINE,
        seed_id=SEED,
        slot_keys=keys,
    )

    first = schedule_due_times(20, _curve({10: 5, 11: 5}), **kwargs)
    second = schedule_due_times(20, _curve({10: 5, 11: 5}), **kwargs)

    # Planner 重跑 / worker 重启得到相同 due_at
    assert first == second


def test_stratified_schedule_slot_keys_drive_distinct_due() -> None:
    keys = [f"comment:obligation-{index}" for index in range(10)]

    planned = schedule_due_times(
        10,
        _curve({10: 1, 11: 1}),
        start_at=START,
        deadline_at=DEADLINE,
        seed_id=SEED,
        slot_keys=keys,
    )

    # 不同义务键得到不同 due（slot 身份决定偏移，非批内位置）
    assert len(set(planned)) == len(planned)


def test_stratified_schedule_same_batch_is_replay_stable() -> None:
    keys = [f"view:message-{index}" for index in range(8)]
    config = _curve({10: 1, 11: 1})

    first = schedule_due_times(8, config, start_at=START, deadline_at=DEADLINE, seed_id=SEED, slot_keys=keys)
    second = schedule_due_times(8, config, start_at=START, deadline_at=DEADLINE, seed_id=SEED, slot_keys=keys)

    # 同批输入（DueSet 相同到期集合）重放得到完全相同的结果；
    # 跨批 period 级 due 稳定依赖 PacingSlot period 计划持久化（下一迭代）
    assert first == second


def test_stratified_schedule_subset_reuses_period_plan_slots() -> None:
    keys = [f"view:message-{index}" for index in range(8)]
    config = _curve({10: 1, 11: 1})
    full = schedule_due_times(
        8,
        config,
        period_start_at=START,
        start_at=START,
        deadline_at=DEADLINE,
        seed_id=SEED,
        slot_keys=keys,
        plan_total=8,
        slot_ordinals=list(range(8)),
    )

    subset = schedule_due_times(
        3,
        config,
        period_start_at=START,
        start_at=START + timedelta(minutes=17),
        deadline_at=DEADLINE,
        seed_id=SEED,
        slot_keys=keys[4:7],
        plan_total=8,
        slot_ordinals=[4, 5, 6],
    )

    assert subset == full[4:7]


def test_stratified_schedule_rejects_implicit_slot_identity_mismatch() -> None:
    with pytest.raises(ValueError, match="slot_identity_length_mismatch"):
        schedule_due_times(
            2,
            {},
            start_at=START,
            deadline_at=DEADLINE,
            seed_id=SEED,
            slot_keys=["only-one"],
        )


def test_stratified_schedule_splits_by_curve_weights() -> None:
    planned = schedule_due_times(
        30,
        _curve({10: 2, 11: 1}),
        start_at=START,
        deadline_at=DEADLINE,
        seed_id=SEED,
    )

    first_hour = [value for value in planned if value < datetime(2026, 8, 15, 11, 0)]
    second_hour = [value for value in planned if value >= datetime(2026, 8, 15, 11, 0)]
    assert 19 <= len(first_hour) <= 21
    assert 9 <= len(second_hour) <= 11


def test_stratified_buckets_use_largest_remainder_not_weight_rank() -> None:
    # 权重 5:3、total=2 → 精确份额 [1.25, 0.75]；floor=[1,0]，余数 0.75>0.25
    # 应补给第二小时（[1,1]），而不是按权重排序给第一小时（[2,0]）。
    planned = schedule_due_times(
        2,
        _curve({10: 5, 11: 3}),
        start_at=START,
        deadline_at=DEADLINE,
        seed_id=SEED,
    )

    first_hour = [value for value in planned if value < datetime(2026, 8, 15, 11, 0)]
    second_hour = [value for value in planned if value >= datetime(2026, 8, 15, 11, 0)]
    assert len(first_hour) == 1
    assert len(second_hour) == 1


def test_stratified_schedule_normalizes_zero_weights_under_nonzero_v1() -> None:
    # nonzero_v1：0 权重小时按总合同归一为低非零权重，不再整段跳过
    config = dict(_curve({10: 2, 11: 0}))
    config["fulfillment_soft_pacing_version"] = "nonzero_v1"

    planned = schedule_due_times(
        3,
        config,
        start_at=START,
        deadline_at=DEADLINE,
        seed_id=SEED,
    )

    assert len(planned) == 3
    first_hour = [value for value in planned if value < datetime(2026, 8, 15, 11, 0)]
    second_hour = [value for value in planned if value >= datetime(2026, 8, 15, 11, 0)]
    # 归一后 11 点权重为 1（2:1），低密度但非零
    assert len(first_hour) == 2
    assert len(second_hour) == 1


def test_stratified_schedule_keeps_low_nonzero_density_in_quiet_hours() -> None:
    # quiet hours 解释为低非零权重：不整段跳过，也不平移到静默结束同一秒
    config = dict(_curve({10: 8, 11: 8}))
    config["fulfillment_soft_pacing_version"] = "nonzero_v1"
    config["quiet_hours"] = {"start": "11:00", "end": "12:00"}

    planned = schedule_due_times(
        9,
        config,
        start_at=START,
        deadline_at=DEADLINE,
        seed_id=SEED,
    )

    assert len(planned) == 9
    second_hour = [value for value in planned if value >= datetime(2026, 8, 15, 11, 0)]
    # quiet 窗口权重 min(8, quiet_threshold=2)=2 → 密度 8:2 → 11 点约 2 条且分散
    assert 1 <= len(second_hour) <= 3
    assert len(set(second_hour)) == len(second_hour)


def test_stratified_schedule_no_same_second_and_jittered() -> None:
    planned = schedule_due_times(
        20,
        {},
        start_at=START,
        deadline_at=DEADLINE,
        seed_id="jitter-test",
    )

    ordered = sorted(planned)
    # 分层：任意两点不同秒（每层最多一个点）
    assert len(set(ordered)) == len(ordered)
    gaps = {
        round((later - earlier).total_seconds(), 3)
        for earlier, later in zip(ordered, ordered[1:])
    }
    # 不等距（非模板步进）
    assert len(gaps) > 5


def test_stratified_schedule_returns_empty_without_active_window() -> None:
    planned = schedule_due_times(
        5,
        _curve({10: 0, 11: 0}),
        start_at=START,
        deadline_at=DEADLINE,
        seed_id=SEED,
    )

    # 显式 shortfall：无活跃小时返回空，调用方按 typed shortfall 处理
    assert planned == []


def test_stratified_schedule_at_or_after_deadline_returns_empty() -> None:
    assert schedule_due_times(
        5,
        {},
        start_at=START,
        deadline_at=START,
        seed_id=SEED,
    ) == []


def test_stratified_schedule_partial_start_bucket_uses_remaining_window() -> None:
    start = datetime(2026, 8, 15, 10, 40)
    planned = schedule_due_times(
        6,
        _curve({10: 1, 11: 1}),
        start_at=start,
        deadline_at=DEADLINE,
        seed_id=SEED,
    )

    assert len(planned) == 6
    first_hour = [value for value in planned if value < datetime(2026, 8, 15, 11, 0)]
    # partial start 不追补 anchor 之前的量：首桶容量按剩余 20 分钟权重分摊
    assert 0 < len(first_hour) < 6
    assert all(value >= start for value in first_hour)


def test_pacing_contract_version_exported() -> None:
    assert pacing.PACING_CONTRACT_VERSION == "deterministic_stratified_v1"
    assert ACCOUNT_SOFT_PACING_POLICY_VERSION == "account_soft_pacing_v1"


# ---------------------------------------------------------------------------
# account soft pacing guard
# ---------------------------------------------------------------------------


def _guard_session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def test_account_guard_returns_none_without_occupancy() -> None:
    with _guard_session() as session:
        assert account_policy_not_before(session, 501, tenant_id=1) is None


def test_account_guard_uses_minimum_gap_with_existing_timeline() -> None:
    with _guard_session() as session:
        anchor = datetime(2026, 8, 15, 10, 0)
        for index in range(3):
            session.add(Action(
                id=f"guard-open-{index}",
                tenant_id=1,
                task_id="task-guard",
                task_type="channel_like",
                action_type="like_message",
                account_id=502,
                status="pending",
                scheduled_at=anchor + timedelta(minutes=index),
            ))
        session.commit()

        desired = anchor + timedelta(minutes=2)
        not_before = account_policy_not_before(session, 502, tenant_id=1, now_value=desired)

        assert not_before == desired + timedelta(seconds=20)


def test_account_guard_floor_applies_to_single_collision() -> None:
    with _guard_session() as session:
        anchor = datetime(2026, 8, 15, 10, 0)
        session.add(Action(
            id="guard-single",
            tenant_id=1,
            task_id="task-guard",
            task_type="channel_like",
            action_type="like_message",
            account_id=503,
            status="pending",
            scheduled_at=anchor,
        ))
        session.commit()

        not_before = account_policy_not_before(session, 503, tenant_id=1, now_value=anchor)

        assert not_before == anchor + timedelta(seconds=20)


def test_effective_claim_at_takes_max() -> None:
    due = datetime(2026, 8, 15, 10, 0)
    later = due + timedelta(minutes=5)

    assert effective_claim_at(due, None) == due
    assert effective_claim_at(due, later) == later
    assert effective_claim_at(due, due - timedelta(seconds=1)) == due


# ---------------------------------------------------------------------------
# comment replacement 不做 future→now
# ---------------------------------------------------------------------------


def test_accelerate_future_replacements_keeps_scheduled_at() -> None:
    from types import SimpleNamespace

    from app.services.task_center.executors.channel_comment_schedule import (
        accelerate_future_replacements,
    )

    future = datetime(2026, 8, 15, 23, 0)
    now_value = datetime(2026, 8, 15, 10, 0)
    action = Action(
        id="replacement-1",
        tenant_id=1,
        task_id="task-comment",
        task_type="channel_comment",
        action_type="post_comment",
        account_id=1,
        status="pending",
        scheduled_at=future,
        payload={"comment_action_attempt_no": 2},
    )
    task = SimpleNamespace(id="task-comment", stats={}, next_run_at=None)

    class _Scalars:
        def __init__(self, items):
            self._items = items

        def __iter__(self):
            return iter(self._items)

    class _Session:
        def scalars(self, _stmt):
            return _Scalars([action])

    changed = accelerate_future_replacements(_Session(), task, now_value=now_value)

    # wake 任务但禁止 future→now：scheduled_at 保持原值
    assert changed == 1
    assert action.scheduled_at == future
    assert task.next_run_at == now_value
