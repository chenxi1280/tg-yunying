from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    AiGroupMessageMemory,
    ContentMixCycle,
    Task,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
    TaskRuntimeActiveBlocker,
    TaskRuntimeSummary,
    Tenant,
)
from app.services import group_listener_admission, group_listeners
from app.services.task_center import (
    ai_generation_dispatch,
    conversation_speaker_rotation,
    details,
    dispatcher,
    runtime_resources,
)
from app.services.task_center.ai_group_prompt import build_group_prompt
from app.services.task_center.executors import group_ai_chat


pytestmark = pytest.mark.no_postgres


def _engine(url: str = "sqlite:///:memory:"):
    engine = create_engine(url, future=True)
    Base.metadata.create_all(engine)
    return engine


def test_speaker_rebind_returns_action_to_generation_worker(monkeypatch) -> None:
    engine = _engine()
    with Session(engine) as session:
        session.add(Tenant(id=1, name="tenant"))
        session.add(Task(id="task-1", tenant_id=1, name="task", type="group_ai_chat", status="running"))
        memory = AiGroupMessageMemory(
            id="memory-a",
            tenant_id=1,
            group_id=7,
            task_id="task-1",
            action_id="action-1",
            account_id=11,
            raw_text="账号 A 的口吻",
            status="reserved",
        )
        action = Action(
            id="action-1",
            tenant_id=1,
            task_id="task-1",
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=11,
            status="executing",
            claim_owner="dispatcher",
            claim_token="claim",
            lease_owner="dispatcher",
            payload={
                "group_id": 7,
                "message_text": "账号 A 的口吻",
                "ai_generation_status": "ready",
                "ai_generation_result_cache": {"contents": ["账号 A 的口吻"]},
                "ai_generation_attempt_id": "attempt-a",
                "ai_generation_request_id": "request-a",
                "ai_message_memory_id": memory.id,
            },
        )
        session.add_all([memory, action])
        session.commit()
        monkeypatch.setattr(dispatcher, "_speaker_rotation_candidates", lambda *_args, **_kwargs: [11, 12])
        monkeypatch.setattr(
            conversation_speaker_rotation,
            "reserve_speaker_turn",
            lambda *_args, **_kwargs: SimpleNamespace(allowed=True, account_id=12, reason="rotated_from_last_speaker"),
        )

        allowed = dispatcher._speaker_rotation_gate_pass(session, action, group_id=7, account_id=11)

        assert allowed is False
        assert action.status == "pending"
        assert action.account_id == 12
        assert action.claim_owner == ""
        assert action.payload["message_text"] == ""
        assert action.payload["ai_generation_status"] == "pending"
        assert action.payload["ai_generation_result_cache"] == {}
        assert action.payload["ai_message_memory_id"] == ""
        assert action.result["generation_stage"] == "speaker_rebind_requeue"
        assert memory.status == "expired_before_send"
        assert memory.result["error_code"] == "speaker_rebound"


def test_speaker_rebind_releases_old_account_runtime_reservation(monkeypatch) -> None:
    engine = _engine()
    with Session(engine) as session:
        session.add(Tenant(id=1, name="tenant"))
        session.add(Task(id="task-1", tenant_id=1, name="task", type="group_ai_chat", status="running"))
        action = Action(
            id="action-runtime-release",
            tenant_id=1,
            task_id="task-1",
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=11,
            status="executing",
            payload={"group_id": 7, "message_text": "旧正文", "ai_generation_status": "ready"},
        )
        session.add(action)
        session.commit()
        monkeypatch.setattr(dispatcher, "_speaker_rotation_candidates", lambda *_args, **_kwargs: [11, 12])
        monkeypatch.setattr(
            conversation_speaker_rotation,
            "reserve_speaker_turn",
            lambda *_args, **_kwargs: SimpleNamespace(
                allowed=True,
                account_id=12,
                reason="rotated_from_last_speaker",
            ),
        )
        runtime_resources._IN_FLIGHT_ACCOUNTS.add(11)
        runtime_resources._ACTION_RESERVATIONS[action.id] = runtime_resources._RuntimeReservation(account_id=11)
        try:
            allowed = dispatcher._speaker_rotation_gate_pass(session, action, group_id=7, account_id=11)

            assert allowed is False
            assert 11 not in runtime_resources._IN_FLIGHT_ACCOUNTS
            assert action.id not in runtime_resources._ACTION_RESERVATIONS
        finally:
            runtime_resources._IN_FLIGHT_ACCOUNTS.discard(11)
            runtime_resources._ACTION_RESERVATIONS.pop(action.id, None)


def test_legacy_context_expiration_default_preserves_explicit_zero() -> None:
    assert group_ai_chat._context_expire_after_messages({}) == 10
    assert group_ai_chat._context_expire_after_messages({"context_expire_after_messages": 0}) == 0
    assert group_ai_chat._context_expire_after_messages({"context_expire_after_messages": 25}) == 25


def test_prompt_consumes_scoped_memory_and_slot_guidance() -> None:
    bundle = build_group_prompt(
        {
            "group_id": 7,
            "account_memories": {"11": "上次觉得节奏太快"},
            "generation_slots": [{
                "sequence_index": 1,
                "slot_id": "slot-1",
                "account_id": 11,
                "act_type": "follow_up",
                "account_profile": "说话直接",
                "topic_direction": {"title": "活动节奏", "description": "讨论安排，不谈价格"},
                "teacher_target": {"name": "成年老师", "description": "穿搭自然"},
                "material_intent": "",
                "content_guidance": "承接上一句，不要总结",
            }],
        },
        target_label="普通交流群",
        history="真人用户：今天节奏有点快",
        count=1,
    )

    serialized = bundle.user_prompt
    assert "上次觉得节奏太快" in serialized
    assert "说话直接" in serialized
    assert "活动节奏" in serialized
    assert "成年老师" in serialized
    assert "承接上一句" in serialized
    assert "价格" not in serialized


def test_quality_stats_refresh_stale_task_before_merge(tmp_path) -> None:
    engine = _engine(f"sqlite:///{tmp_path / 'quality-stats.db'}")
    with Session(engine) as seed:
        seed.add(Tenant(id=1, name="tenant"))
        seed.add(Task(id="task-1", tenant_id=1, name="task", type="group_ai_chat", status="running", stats={"base": 1}))
        seed.commit()
    first = Session(engine)
    stale = first.get(Task, "task-1")
    with Session(engine) as second:
        current = second.get(Task, "task-1")
        current.stats = {**current.stats, "other_action_count": 1}
        second.commit()
    action = Action(id="action-1", tenant_id=1, task_id="task-1", task_type="group_ai_chat", action_type="send_message")

    ai_generation_dispatch._record_quality_event(stale, action, "current_action_count", blocker="rule_binding_missing")
    first.commit()
    first.close()

    with Session(engine) as verify:
        stats = verify.get(Task, "task-1").stats
        assert stats["base"] == 1
        assert stats["other_action_count"] == 1
        assert "current_action_count" not in stats
        assert "conversation_quality_active_blockers" not in stats
        summary = verify.scalar(select(TaskRuntimeSummary).where(
            TaskRuntimeSummary.task_id == "task-1"
        ))
        blocker = verify.scalar(select(TaskRuntimeActiveBlocker).where(
            TaskRuntimeActiveBlocker.task_id == "task-1"
        ))
        assert summary.summary["quality_event_counts"]["current_action_count"] == 1
        assert blocker.blocker_code == "rule_binding_missing"


def test_quality_stats_merge_keeps_local_and_concurrent_changes(tmp_path) -> None:
    engine = _engine(f"sqlite:///{tmp_path / 'quality-stats-local.db'}")
    with Session(engine) as seed:
        seed.add(Tenant(id=1, name="tenant"))
        seed.add(Task(id="task-1", tenant_id=1, name="task", type="group_ai_chat", status="running", stats={"base": 1}))
        seed.commit()
    first = Session(engine)
    stale = first.get(Task, "task-1")
    with Session(engine) as second:
        current = second.get(Task, "task-1")
        current.stats = {**current.stats, "concurrent_count": 1}
        second.commit()
    action = Action(id="action-1", tenant_id=1, task_id="task-1", task_type="group_ai_chat", action_type="send_message")

    ai_generation_dispatch._record_quality_event(stale, action, "current_action_count")
    first.commit()
    first.close()

    with Session(engine) as verify:
        stats = verify.get(Task, "task-1").stats
        assert stats["base"] == 1
        assert stats["concurrent_count"] == 1
        assert "current_action_count" not in stats
        summary = verify.scalar(select(TaskRuntimeSummary).where(
            TaskRuntimeSummary.task_id == "task-1"
        ))
        assert summary.summary["quality_event_counts"]["current_action_count"] == 1


def test_ai_acceptance_combines_authoritative_daily_dimensions() -> None:
    engine = _engine()
    today = datetime.now(UTC).date()
    with Session(engine) as session:
        session.add(Tenant(id=1, name="tenant"))
        task = Task(
            id="task-1",
            tenant_id=1,
            name="task",
            type="group_ai_chat",
            status="running",
            timezone="UTC",
            stats={"conversation_quality_e4_passed": True},
        )
        ledger = TaskDayLedger(
            id="ledger-1",
            tenant_id=1,
            task_id=task.id,
            timezone_snapshot="Asia/Shanghai",
            timezone_revision=1,
            obligation_local_date=today,
            period_start_at=datetime.now(UTC),
            deadline_at=datetime.now(UTC),
            day_phase="active",
            planning_anchor_at=datetime.now(UTC),
        )
        quantity = TaskGroupDailyMessageSlot(
            id="quantity-1",
            tenant_id=1,
            task_id=task.id,
            task_day_ledger_id=ledger.id,
            target_operation_target_id=7,
            slot_kind="extra_volume",
            slot_ordinal=1,
            state="confirmed",
        )
        cycle = ContentMixCycle(
            id="cycle-1",
            tenant_id=1,
            task_id=task.id,
            target_operation_target_id=7,
            task_day_ledger_id=ledger.id,
            cycle_seq=1,
            config_revision=1,
            scope_total_slots=1,
            allocation_seed="seed",
            allocation_closed_at=datetime.now(UTC),
            settlement_status="settled",
            settlement_outcome="met",
        )
        session.add_all([task, ledger, quantity, cycle])
        session.commit()

        statuses = details._ai_acceptance_statuses(session, task, task.stats)
        assert statuses == {
            "quantity_status": "met",
            "content_mix_status": "met",
            "conversation_quality_status": "met",
            "acceptance_status": "met",
        }

        cycle.settlement_outcome = "shortfall"
        session.flush()
        statuses = details._ai_acceptance_statuses(session, task, task.stats)
        assert statuses["content_mix_status"] == "missed"
        assert statuses["acceptance_status"] == "missed"


def test_listener_cursor_bootstrap_contiguous_and_gap() -> None:
    group = SimpleNamespace(listener_remote_cursor="", listener_cursor_status="unproven")
    snapshots = lambda *ids: [SimpleNamespace(remote_message_id=str(value)) for value in ids]

    group_listeners.update_listener_cursor(group, snapshots(100, 101))
    assert (group.listener_remote_cursor, group.listener_cursor_status) == ("101", "contiguous")

    group_listeners.update_listener_cursor(group, snapshots(101, 102))
    assert (group.listener_remote_cursor, group.listener_cursor_status) == ("102", "contiguous")

    group_listeners.update_listener_cursor(group, snapshots(105, 106))
    assert (group.listener_remote_cursor, group.listener_cursor_status) == ("102", "gap")


def test_listener_cursor_non_numeric_window_remains_unproven() -> None:
    group = SimpleNamespace(listener_remote_cursor="", listener_cursor_status="unproven")
    group_listeners.update_listener_cursor(group, [SimpleNamespace(remote_message_id="mock-id")])
    assert group.listener_remote_cursor == ""
    assert group.listener_cursor_status == "unproven"


def test_listener_cursor_recovers_gap_with_anchored_pages() -> None:
    group = SimpleNamespace(listener_remote_cursor="102", listener_cursor_status="gap")
    snapshots = lambda *ids: [SimpleNamespace(remote_message_id=str(value)) for value in ids]

    group_listeners.update_listener_cursor(
        group,
        snapshots(103, 104),
        after_message_id=102,
        fetch_limit=2,
    )
    assert (group.listener_remote_cursor, group.listener_cursor_status) == ("104", "unproven")

    group_listeners.update_listener_cursor(
        group,
        [],
        after_message_id=104,
        fetch_limit=2,
    )
    assert (group.listener_remote_cursor, group.listener_cursor_status) == ("104", "contiguous")


def test_listener_fetch_anchors_at_persisted_numeric_cursor(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fetch(*_args, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(group_listener_admission.gateway, "fetch_group_messages", fetch)
    group = SimpleNamespace(
        listener_remote_cursor="102",
        listener_context_limit=20,
        tg_peer_id="-1007",
    )
    account = SimpleNamespace(id=11, session_ciphertext="session")

    assert group_listener_admission.fetch_listener_snapshots(
        None,
        group=group,
        account=account,
        credentials=object(),
    ) == []
    assert captured["after_message_id"] == 102


def test_listener_fetch_drains_full_anchored_pages_until_short(monkeypatch) -> None:
    calls: list[int] = []

    def fetch(*_args, **kwargs):
        after = int(kwargs["after_message_id"])
        calls.append(after)
        values = {102: (103, 104), 104: (105,)}[after]
        return [SimpleNamespace(remote_message_id=str(value)) for value in values]

    monkeypatch.setattr(group_listener_admission.gateway, "fetch_group_messages", fetch)
    group = SimpleNamespace(
        listener_remote_cursor="102",
        listener_context_limit=2,
        tg_peer_id="-1007",
    )
    account = SimpleNamespace(id=11, session_ciphertext="session")

    pages = group_listener_admission.fetch_listener_snapshot_pages(
        None,
        group=group,
        account=account,
        credentials=object(),
    )

    assert calls == [102, 104]
    assert [[item.remote_message_id for item in page] for page in pages] == [
        ["103", "104"],
        ["105"],
    ]

    cursor_group = SimpleNamespace(
        listener_remote_cursor="102",
        listener_cursor_status="gap",
        listener_context_limit=2,
    )
    group_listeners._advance_listener_cursor_pages(cursor_group, pages, 102)
    assert (cursor_group.listener_remote_cursor, cursor_group.listener_cursor_status) == (
        "105",
        "contiguous",
    )


def test_listener_anchored_mixed_cursor_window_stays_unproven() -> None:
    group = SimpleNamespace(listener_remote_cursor="102", listener_cursor_status="gap")

    group_listeners.update_listener_cursor(
        group,
        [SimpleNamespace(remote_message_id="103"), SimpleNamespace(remote_message_id="mock-id")],
        after_message_id=102,
        fetch_limit=20,
    )

    assert group.listener_remote_cursor == "102"
    assert group.listener_cursor_status == "unproven"
