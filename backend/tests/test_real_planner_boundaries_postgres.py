from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import delete, func, select

from app.database import Base, SessionLocal, engine
from app.models import (
    Action,
    AiAccountVoiceProfile,
    GroupContextMessage,
    MessageFingerprint,
    OperationTarget,
    RuleSet,
    RuleSetVersion,
    SchedulingSetting,
    Task,
    TaskAccountDailyCoverage,
    TaskDailyCoveragePlanCursor,
    TaskMembershipAdmissionItem,
    Tenant,
    TgAccount,
    TgAccountOnlineState,
    TgGroup,
    TgGroupAccount,
)
from app.services._common import _now
from app.services.task_center import service as task_service
from app.services.task_center.executors import group_ai_chat
from tests.real_planner_boundaries_test_support import configure_real_planner_test


pytestmark = pytest.mark.allow_missing_rule_binding
TENANT_ID = 913_793
ACCOUNT_BASE = 913_793_000
TASK_ID = "pg-coverage-cursor-task"
GROUP_ID = 913_793
ACCOUNT_COUNT = 580
RULE_SET_ID = 913_793
RULE_VERSION_ID = 913_794


@pytest.mark.parametrize(
    (
        "messages_per_round",
        "drain_all",
        "expected_rounds",
        "expected_transaction_chunks",
        "minimum_cursor_version",
    ),
    [
        (10, False, [10], [10], 1),
        (30, False, [30], [20, 10], 2),
        (60, True, [60] * 9, [20, 20, 20], 29),
    ],
)
def test_postgres_real_planner_preserves_round_sizes_and_drains_580(
    monkeypatch,
    messages_per_round: int,
    drain_all: bool,
    expected_rounds: list[int],
    expected_transaction_chunks: list[int],
    minimum_cursor_version: int,
) -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    timestamp = _now().replace(hour=23, minute=59 if drain_all else 30, second=0, microsecond=0)
    transaction_chunks: list[int] = []
    configure_real_planner_test(monkeypatch, timestamp, transaction_chunks)
    # This boundary fixture isolates cursor chunking from the time-dependent capacity policy.
    monkeypatch.setattr(group_ai_chat, "_coverage_capacity_blocker", lambda *_args, **_kwargs: {})
    try:
        _seed_real_planner_scope(messages_per_round, timestamp)
        actual_rounds = _run_real_planner_until(ACCOUNT_COUNT) if drain_all else _run_real_planner_rounds(1)
        with SessionLocal() as session:
            cursor = session.scalar(select(TaskDailyCoveragePlanCursor).where(TaskDailyCoveragePlanCursor.task_id == TASK_ID))
            reserved = session.scalar(select(func.count()).select_from(TaskAccountDailyCoverage).where(
                TaskAccountDailyCoverage.task_id == TASK_ID,
                TaskAccountDailyCoverage.state == "reserved",
            ))
            task = session.get(Task, TASK_ID)
            coverage_states = session.execute(
                select(TaskAccountDailyCoverage.state, TaskAccountDailyCoverage.blocker_code, func.count())
                .where(TaskAccountDailyCoverage.task_id == TASK_ID)
                .group_by(TaskAccountDailyCoverage.state, TaskAccountDailyCoverage.blocker_code)
            ).all()

        assert actual_rounds[:len(expected_rounds)] == expected_rounds
        assert transaction_chunks[:len(expected_transaction_chunks)] == expected_transaction_chunks
        assert all(0 < count <= 20 for count in transaction_chunks)
        assert all(0 < count <= messages_per_round for count in actual_rounds)
        assert reserved == (ACCOUNT_COUNT if drain_all else sum(expected_rounds)), (
            task.last_error, task.stats, coverage_states,
        )
        assert sum(actual_rounds) == reserved
        assert minimum_cursor_version <= cursor.version <= reserved
    finally:
        _cleanup()


def test_postgres_planner_phase_a_creates_pending_actions_for_dispatcher(monkeypatch) -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    timestamp = _now().replace(hour=23, minute=30, second=0, microsecond=0)
    monkeypatch.setattr(group_ai_chat, "_now", lambda: timestamp)

    def forbidden_external(*_args, **_kwargs):
        raise AssertionError("Planner Phase A must not call external services")

    monkeypatch.setattr("app.services.group_listeners.collect_group_context", forbidden_external)
    monkeypatch.setattr("app.services.task_center.ai_generator.generate_group_messages", forbidden_external)
    monkeypatch.setattr("app.services.task_center.ai_generator.generate_group_reply_messages", forbidden_external)
    try:
        _seed_real_planner_scope(10, timestamp)
        with SessionLocal() as session:
            task = session.get(Task, TASK_ID)
            task.type_config = {**task.type_config, "reply_min_per_round": 1}
            session.add(GroupContextMessage(
                tenant_id=TENANT_ID,
                group_id=GROUP_ID,
                listener_account_id=ACCOUNT_BASE,
                sender_name="真人用户",
                content="今天群里聊什么？",
                remote_message_id="9001",
                sent_at=timestamp - timedelta(minutes=1),
            ))
            session.commit()

        assert _run_real_planner_rounds(1) == [10]
        with SessionLocal() as session:
            actions = list(session.scalars(select(Action).where(Action.task_id == TASK_ID)))
            cursor = session.scalar(select(TaskDailyCoveragePlanCursor).where(
                TaskDailyCoveragePlanCursor.task_id == TASK_ID,
            ))
            reserved = session.scalar(select(func.count()).select_from(TaskAccountDailyCoverage).where(
                TaskAccountDailyCoverage.task_id == TASK_ID,
                TaskAccountDailyCoverage.state == "reserved",
            ))

        assert len(actions) == reserved == 10
        assert cursor.version >= 1
        assert all(action.status == "pending" for action in actions)
        assert sum(bool(action.payload["reply_to_message_id"]) for action in actions) == 1
        assert all(action.payload["ai_generation_status"] == "pending" for action in actions)
        assert all(not action.payload["message_text"] for action in actions)
    finally:
        _cleanup()


def test_postgres_planner_phase_a_rolls_back_actions_coverage_and_cursor(monkeypatch) -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    timestamp = _now().replace(hour=23, minute=30, second=0, microsecond=0)
    monkeypatch.setattr(group_ai_chat, "_now", lambda: timestamp)
    original_reserve = group_ai_chat._reserve_action_coverage
    calls = 0

    def fail_second_reservation(session, action, payload):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("phase-a-reservation-injected-failure")
        return original_reserve(session, action, payload)

    monkeypatch.setattr(group_ai_chat, "_reserve_action_coverage", fail_second_reservation)
    try:
        _seed_real_planner_scope(10, timestamp)
        with pytest.raises(RuntimeError, match="phase-a-reservation-injected-failure"):
            with SessionLocal() as session:
                group_ai_chat.build_plan(session, session.get(Task, TASK_ID))
        with SessionLocal() as session:
            actions = session.scalar(select(func.count()).select_from(Action).where(Action.task_id == TASK_ID))
            reserved = session.scalar(select(func.count()).select_from(TaskAccountDailyCoverage).where(
                TaskAccountDailyCoverage.task_id == TASK_ID,
                TaskAccountDailyCoverage.state == "reserved",
            ))
            cursor = session.scalar(select(TaskDailyCoveragePlanCursor).where(
                TaskDailyCoveragePlanCursor.task_id == TASK_ID,
            ))
        assert calls == 2
        assert actions == reserved == 0
        assert cursor is None or (
            cursor.version == 0
            and not cursor.last_coverage_id
            and cursor.last_account_id is None
        )
    finally:
        _cleanup()


def _seed_real_planner_scope(messages_per_round: int, timestamp) -> None:
    with SessionLocal() as session:
        session.add(Tenant(id=TENANT_ID, name="real-planner"))
        session.commit()
        session.add(RuleSet(
            id=RULE_SET_ID,
            tenant_id=TENANT_ID,
            name="runtime rule",
            status="active",
            task_types=["group_ai_chat"],
            active_version_id=RULE_VERSION_ID,
        ))
        session.commit()
        session.add(RuleSetVersion(
            id=RULE_VERSION_ID,
            tenant_id=TENANT_ID,
            rule_set_id=RULE_SET_ID,
            version=1,
            status="published",
        ))
        session.add(TgGroup(
            id=GROUP_ID,
            tenant_id=TENANT_ID,
            tg_peer_id="-100913793",
            title="real coverage",
            auth_status="已授权运营",
            active_window="00:00-23:59",
            daily_limit=5_000,
            account_cooldown_seconds=0,
            group_cooldown_seconds=0,
            require_review=False,
        ))
        session.add(_real_planner_task(messages_per_round, timestamp))
        session.commit()
        session.bulk_insert_mappings(TgAccount, [_real_account_mapping(index) for index in range(ACCOUNT_COUNT)])
        session.commit()
        session.bulk_insert_mappings(TgGroupAccount, [_group_account_mapping(index) for index in range(ACCOUNT_COUNT)])
        session.bulk_insert_mappings(TgAccountOnlineState, [_online_state_mapping(index, timestamp) for index in range(ACCOUNT_COUNT)])
        session.bulk_insert_mappings(AiAccountVoiceProfile, [_voice_profile_mapping(index) for index in range(ACCOUNT_COUNT)])
        session.bulk_insert_mappings(TaskAccountDailyCoverage, [_coverage_mapping(index, timestamp) for index in range(ACCOUNT_COUNT)])
        session.commit()


def _real_planner_task(messages_per_round: int, timestamp) -> Task:
    return Task(
        id=TASK_ID,
        tenant_id=TENANT_ID,
        name=f"real planner {messages_per_round}",
        type="group_ai_chat",
        status="running",
        next_run_at=timestamp - timedelta(minutes=1),
        account_config={"selection_mode": "all", "max_concurrent": ACCOUNT_COUNT, "cooldown_per_account_minutes": 0},
        pacing_config={"max_actions_per_hour": 5_000, "interval_seconds_min": 0, "interval_seconds_max": 0},
        type_config={
            "target_group_id": GROUP_ID,
            "account_coverage_mode": "all_accounts_daily",
            "per_account_daily_min_messages": 1,
            "messages_per_round_mode": "manual",
            "messages_per_round": messages_per_round,
            "reply_min_per_round": 0,
            "fact_anchor_required": False,
            "rule_set_version_id": RULE_VERSION_ID,
        },
    )


def _real_account_mapping(index: int) -> dict:
    account_id = ACCOUNT_BASE + index
    return {
        "id": account_id,
        "tenant_id": TENANT_ID,
        "display_name": f"account-{index}",
        "phone_masked": str(account_id),
        "account_identity": "normal",
        "status": "在线",
        "health_score": 100,
        "session_ciphertext": "session",
    }


def _group_account_mapping(index: int) -> dict:
    return {
        "tenant_id": TENANT_ID,
        "group_id": GROUP_ID,
        "account_id": ACCOUNT_BASE + index,
        "permission_label": "可发言",
        "can_send": True,
    }


def _online_state_mapping(index: int, timestamp) -> dict:
    return {
        "tenant_id": TENANT_ID,
        "account_id": ACCOUNT_BASE + index,
        "desired_online": True,
        "online_status": "online",
        "last_seen_at": timestamp,
        "stale_after_at": timestamp + timedelta(minutes=10),
    }


def _coverage_mapping(index: int, timestamp) -> dict:
    return {
        "tenant_id": TENANT_ID,
        "task_id": TASK_ID,
        "group_id": GROUP_ID,
        "account_id": ACCOUNT_BASE + index,
        "coverage_date": timestamp.date(),
        "target_count": 1,
        "confirmed_count": 0,
        "state": "ready",
        "targeted_at": timestamp - timedelta(minutes=1),
    }


def _voice_profile_mapping(index: int) -> dict:
    return {
        "tenant_id": TENANT_ID,
        "account_id": ACCOUNT_BASE + index,
        "version": 1,
        "status": "active",
        "quality_status": "active",
        "short_prompt_summary": "自然短句，少表情",
    }


def _run_real_planner_rounds(round_count: int) -> list[int]:
    planned_rounds: list[int] = []
    for _index in range(round_count):
        with SessionLocal() as session:
            before = session.scalar(select(func.count()).select_from(Action).where(Action.task_id == TASK_ID)) or 0
        task_service._plan_due_task(SessionLocal, TASK_ID, None, limit=100)
        with SessionLocal() as session:
            after = session.scalar(select(func.count()).select_from(Action).where(Action.task_id == TASK_ID)) or 0
        planned_rounds.append(after - before)
    return planned_rounds


def _run_real_planner_until(target_count: int) -> list[int]:
    planned_rounds: list[int] = []
    while sum(planned_rounds) < target_count:
        planned = _run_real_planner_rounds(1)[0]
        if planned <= 0:
            break
        planned_rounds.append(planned)
    return planned_rounds


def _cleanup() -> None:
    with SessionLocal() as session:
        session.execute(delete(TaskAccountDailyCoverage).where(TaskAccountDailyCoverage.tenant_id == TENANT_ID))
        session.execute(delete(TaskDailyCoveragePlanCursor).where(TaskDailyCoveragePlanCursor.tenant_id == TENANT_ID))
        session.execute(delete(TaskMembershipAdmissionItem).where(TaskMembershipAdmissionItem.tenant_id == TENANT_ID))
        session.execute(delete(OperationTarget).where(OperationTarget.tenant_id == TENANT_ID))
        session.execute(delete(Action).where(Action.tenant_id == TENANT_ID))
        session.execute(delete(AiAccountVoiceProfile).where(AiAccountVoiceProfile.tenant_id == TENANT_ID))
        session.execute(delete(GroupContextMessage).where(GroupContextMessage.tenant_id == TENANT_ID))
        session.execute(delete(MessageFingerprint).where(MessageFingerprint.tenant_id == TENANT_ID))
        session.execute(delete(TgAccountOnlineState).where(TgAccountOnlineState.tenant_id == TENANT_ID))
        session.execute(delete(TgGroupAccount).where(TgGroupAccount.tenant_id == TENANT_ID))
        session.execute(delete(Task).where(Task.tenant_id == TENANT_ID))
        session.execute(delete(RuleSetVersion).where(RuleSetVersion.tenant_id == TENANT_ID))
        session.execute(delete(RuleSet).where(RuleSet.tenant_id == TENANT_ID))
        session.execute(delete(SchedulingSetting).where(SchedulingSetting.tenant_id == TENANT_ID))
        session.execute(delete(TgGroup).where(TgGroup.tenant_id == TENANT_ID))
        session.execute(delete(TgAccount).where(TgAccount.tenant_id == TENANT_ID))
        session.execute(delete(Tenant).where(Tenant.id == TENANT_ID))
        session.commit()
