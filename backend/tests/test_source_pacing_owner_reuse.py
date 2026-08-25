from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    ExecutionAttempt,
    OperationTarget,
    SourcePacingAdmission,
    SourcePacingState,
    Task,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
    Tenant,
    TgAccount,
)
from app.services.task_center.ai_pacing_takeover import release_safe_ai_pacing_owners
from app.services.task_center.pacing import PACING_CONTRACT_VERSION
from app.services.task_center.source_pacing_admission import admit_source_paced_attempt


NOW = datetime(2026, 8, 18, 10, 0)
SOURCE_GAP_SECONDS = 864
TENANT_ID = 990_002
ACCOUNT_ID = 990_002
TARGET_ID = 990_020


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        current.add(Tenant(id=TENANT_ID, name="tenant"))
        current.add(TgAccount(
            id=ACCOUNT_ID,
            tenant_id=TENANT_ID,
            display_name="account",
            phone_masked="***0001",
            status="在线",
        ))
        current.add(OperationTarget(
            id=TARGET_ID,
            tenant_id=TENANT_ID,
            target_type="group",
            tg_peer_id="-1009001",
            title="group",
        ))
        current.commit()
        yield current


@pytest.mark.no_postgres
def test_replacement_reuses_stable_owner_pre_gateway_reservation(
    session: Session,
) -> None:
    due_at = NOW + timedelta(seconds=SOURCE_GAP_SECONDS)
    task, ledger, slot = _owner_entities("owner-task", "owner-slot", due_at)
    original = _action("original-action", task.id, slot.id, due_at)
    session.add_all([task, ledger, slot, original])
    session.flush()
    original_attempt = _attempt(original, 1, NOW)
    session.add(original_attempt)
    session.flush()
    assert not admit_source_paced_attempt(session, original, original_attempt, now_value=NOW)
    admission = session.scalar(select(SourcePacingAdmission))
    original.status = "failed"

    replacement = _action("replacement-action", task.id, slot.id, due_at)
    session.add(replacement)
    session.flush()
    replacement_attempt = _attempt(replacement, 1, due_at)
    session.add(replacement_attempt)
    session.flush()

    assert admit_source_paced_attempt(
        session,
        replacement,
        replacement_attempt,
        now_value=due_at,
    )
    assert session.scalar(select(func.count(SourcePacingAdmission.id))) == 1
    assert admission.action_id == replacement.id
    assert admission.attempt_id == replacement_attempt.id


@pytest.mark.no_postgres
def test_overdue_reservations_still_apply_actual_source_gap(
    session: Session,
) -> None:
    first = _add_paced_action(session, "gap-task-a", "gap-slot-a", "gap-action-a")
    second = _add_paced_action(session, "gap-task-b", "gap-slot-b", "gap-action-b")
    before_due = NOW - timedelta(seconds=1)
    assert not admit_source_paced_attempt(session, first[0], first[1], now_value=before_due)
    assert not admit_source_paced_attempt(session, second[0], second[1], now_value=before_due)
    overdue = NOW + timedelta(seconds=SOURCE_GAP_SECONDS * 2)

    first_retry = _attempt(first[0], 2, overdue)
    session.add(first_retry)
    session.flush()
    assert admit_source_paced_attempt(session, first[0], first_retry, now_value=overdue)

    second_retry = _attempt(second[0], 2, overdue)
    session.add(second_retry)
    session.flush()
    assert not admit_source_paced_attempt(session, second[0], second_retry, now_value=overdue)
    expected = overdue + timedelta(seconds=SOURCE_GAP_SECONDS)
    assert second[0].scheduled_at == expected
    second_admission = session.scalar(select(SourcePacingAdmission).where(
        SourcePacingAdmission.action_id == second[0].id,
    ))
    assert second_admission.call_not_before_at == expected


@pytest.mark.no_postgres
def test_replacement_retry_stays_pinned_to_first_owner_admission(
    session: Session,
) -> None:
    original, duplicate, state = _seed_duplicate_owner_admissions(session)
    original.status = "failed"
    duplicate.status = "failed"
    replacement = _action("pinned-replacement", original.task_id, original.primary_quantity_slot_id, NOW)
    session.add(replacement)
    session.flush()

    first_attempt = _attempt(replacement, 1, NOW + timedelta(seconds=1))
    session.add(first_attempt)
    session.flush()
    assert not admit_source_paced_attempt(session, replacement, first_attempt, now_value=first_attempt.before_call_at)
    pinned = session.scalar(select(SourcePacingAdmission).where(
        SourcePacingAdmission.action_id == replacement.id,
    ))

    retry = _attempt(replacement, 2, NOW + timedelta(seconds=2))
    session.add(retry)
    session.flush()
    assert not admit_source_paced_attempt(session, replacement, retry, now_value=retry.before_call_at)
    assert pinned.attempt_id == retry.id
    assert session.scalar(select(func.count(SourcePacingAdmission.id)).where(
        SourcePacingAdmission.action_id == replacement.id,
    )) == 1
    assert state.last_call_started_at == NOW


@pytest.mark.no_postgres
def test_pause_cleanup_releases_only_safe_orphan_owner(session: Session) -> None:
    due_at = NOW - timedelta(hours=2)
    task, ledger, slot = _owner_entities("takeover-task", "takeover-slot", due_at)
    task.task_lifecycle_epoch = 6
    slot.task_lifecycle_epoch = 2
    slot.pacing_period_key = ledger.id
    slot.pacing_source_key_hash = "b" * 64
    slot.release_not_before_at = NOW - timedelta(hours=1)
    action = _action("takeover-skipped", task.id, slot.id, due_at)
    action.status = "skipped"
    session.add_all([task, ledger, slot, action])
    session.flush()

    released = release_safe_ai_pacing_owners(session, task, observed_at=NOW)

    assert released == 1
    assert slot.task_lifecycle_epoch is None
    assert slot.release_not_before_at is None
    assert slot.pacing_due_at == due_at
    assert slot.pacing_plan_hash == "a" * 64
    assert slot.pacing_slot_ordinal == 1


@pytest.mark.no_postgres
def test_pause_cleanup_preserves_future_release_for_safe_owner(
    session: Session,
) -> None:
    future_release = NOW + timedelta(hours=1)
    task, ledger, slot = _owner_entities(
        "future-release-task",
        "future-release-slot",
        NOW - timedelta(hours=1),
    )
    task.task_lifecycle_epoch = 6
    slot.task_lifecycle_epoch = 2
    slot.pacing_period_key = ledger.id
    slot.pacing_source_key_hash = "b" * 64
    slot.release_not_before_at = future_release
    session.add_all([task, ledger, slot])
    session.flush()

    released = release_safe_ai_pacing_owners(session, task, observed_at=NOW)

    assert released == 1
    assert slot.task_lifecycle_epoch is None
    assert slot.release_not_before_at == future_release


@pytest.mark.no_postgres
@pytest.mark.parametrize(
    "case",
    (
        {"status": "retryable_failed", "gateway_started": False, "remote_message_id": ""},
        {"status": "success", "gateway_started": True, "remote_message_id": "1001"},
        {"status": "failed", "gateway_started": True, "remote_message_id": ""},
        {"status": "unknown_after_send", "gateway_started": False, "remote_message_id": ""},
    ),
)
def test_pause_cleanup_preserves_remote_or_active_owner(
    session: Session,
    case: dict,
) -> None:
    status = case["status"]
    gateway_started = case["gateway_started"]
    remote_message_id = case["remote_message_id"]
    suffix = status.replace("_", "-")
    task, ledger, slot = _owner_entities(
        f"preserve-{suffix}",
        f"preserve-slot-{suffix}",
        NOW,
    )
    task.task_lifecycle_epoch = 6
    slot.task_lifecycle_epoch = 2
    slot.pacing_period_key = ledger.id
    slot.pacing_source_key_hash = "b" * 64
    action = _action(f"preserve-action-{suffix}", task.id, slot.id, NOW)
    action.status = status
    session.add_all([task, ledger, slot, action])
    session.flush()
    if gateway_started or remote_message_id:
        attempt = _attempt(action, 1, NOW)
        attempt.gateway_call_started_at = NOW if gateway_started else None
        attempt.remote_message_id = remote_message_id
        session.add(attempt)
        session.flush()

    released = release_safe_ai_pacing_owners(session, task, observed_at=NOW)

    assert released == 0
    assert slot.task_lifecycle_epoch == 2
    assert slot.release_not_before_at == NOW


def test_postgres_owner_reuse_executes_with_targeted_row_lock() -> None:
    from app.database import SessionLocal

    with SessionLocal() as current:
        current.add(Tenant(id=TENANT_ID, name="owner-reuse-postgres"))
        current.flush()
        current.add(TgAccount(
            id=ACCOUNT_ID,
            tenant_id=TENANT_ID,
            display_name="account",
            phone_masked="***0001",
            status="在线",
        ))
        current.add(OperationTarget(
            id=TARGET_ID,
            tenant_id=TENANT_ID,
            target_type="group",
            tg_peer_id="-1009001",
            title="group",
        ))
        current.flush()
        due_at = NOW + timedelta(seconds=SOURCE_GAP_SECONDS)
        task, ledger, slot = _owner_entities("pg-owner-task", "pg-owner-slot", due_at)
        original = _action("pg-original-action", task.id, slot.id, due_at)
        current.add(task)
        current.flush()
        current.add(ledger)
        current.flush()
        current.add(slot)
        current.flush()
        current.add(original)
        current.flush()
        original_attempt = _attempt(original, 1, NOW)
        current.add(original_attempt)
        current.flush()
        assert not admit_source_paced_attempt(current, original, original_attempt, now_value=NOW)
        original.status = "failed"
        replacement = _action("pg-replacement-action", task.id, slot.id, due_at)
        current.add(replacement)
        current.flush()
        replacement_attempt = _attempt(replacement, 1, due_at)
        current.add(replacement_attempt)
        current.flush()
        assert admit_source_paced_attempt(
            current,
            replacement,
            replacement_attempt,
            now_value=due_at,
        )
        assert current.scalar(select(func.count(SourcePacingAdmission.id))) == 1


def test_postgres_pause_cleanup_locks_and_releases_safe_owner() -> None:
    from app.database import SessionLocal

    with SessionLocal() as current:
        current.add(Tenant(id=TENANT_ID, name="takeover-postgres"))
        current.flush()
        current.add(TgAccount(
            id=ACCOUNT_ID,
            tenant_id=TENANT_ID,
            display_name="account",
            phone_masked="***0001",
            status="在线",
        ))
        current.add(OperationTarget(
            id=TARGET_ID,
            tenant_id=TENANT_ID,
            target_type="group",
            tg_peer_id="-1009001",
            title="group",
        ))
        current.flush()
        task, ledger, slot = _owner_entities(
            "pg-takeover-task",
            "pg-takeover-slot",
            NOW - timedelta(hours=2),
        )
        task.task_lifecycle_epoch = 6
        slot.task_lifecycle_epoch = 2
        slot.release_not_before_at = NOW - timedelta(hours=1)
        action = _action("pg-takeover-action", task.id, slot.id, slot.pacing_due_at)
        action.status = "skipped"
        current.add(task)
        current.flush()
        current.add(ledger)
        current.flush()
        current.add(slot)
        current.flush()
        current.add(action)
        current.flush()

        released = release_safe_ai_pacing_owners(current, task, observed_at=NOW)

        assert released == 1
        assert slot.task_lifecycle_epoch is None
        assert slot.release_not_before_at is None


def _add_paced_action(
    session: Session,
    task_id: str,
    slot_id: str,
    action_id: str,
) -> tuple[Action, ExecutionAttempt]:
    task, ledger, slot = _owner_entities(task_id, slot_id, NOW)
    action = _action(action_id, task_id, slot_id, NOW)
    session.add_all([task, ledger, slot, action])
    session.flush()
    attempt = _attempt(action, 1, NOW - timedelta(seconds=1))
    session.add(attempt)
    session.flush()
    return action, attempt


def _seed_duplicate_owner_admissions(
    session: Session,
) -> tuple[Action, Action, SourcePacingState]:
    original, original_attempt = _add_paced_action(
        session,
        "pin-task",
        "pin-slot",
        "pin-original",
    )
    assert not admit_source_paced_attempt(
        session,
        original,
        original_attempt,
        now_value=original_attempt.before_call_at,
    )
    original.status = "success"
    duplicate = _action("pin-duplicate", original.task_id, original.primary_quantity_slot_id, NOW)
    session.add(duplicate)
    session.flush()
    attempt = _attempt(duplicate, 1, NOW - timedelta(seconds=1))
    session.add(attempt)
    session.flush()
    assert not admit_source_paced_attempt(session, duplicate, attempt, now_value=attempt.before_call_at)
    admissions = list(session.scalars(select(SourcePacingAdmission).order_by(
        SourcePacingAdmission.created_at,
        SourcePacingAdmission.id,
    )))
    assert len(admissions) == 2
    admissions[1].call_not_before_at = NOW + timedelta(seconds=1)
    state = session.get(SourcePacingState, admissions[0].source_pacing_state_id)
    assert state is not None
    state.last_call_started_at = NOW
    state.last_source_gap_seconds = SOURCE_GAP_SECONDS
    session.flush()
    return original, duplicate, state


def _owner_entities(
    task_id: str,
    slot_id: str,
    due_at: datetime,
) -> tuple[Task, TaskDayLedger, TaskGroupDailyMessageSlot]:
    task = Task(
        id=task_id,
        tenant_id=TENANT_ID,
        name=task_id,
        type="group_ai_chat",
        status="running",
    )
    ledger = TaskDayLedger(
        id=f"ledger-{task_id}",
        tenant_id=TENANT_ID,
        task_id=task_id,
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=date(2026, 8, 18),
        period_start_at=NOW,
        deadline_at=NOW + timedelta(days=1),
        day_phase="active",
        planning_anchor_at=NOW,
    )
    slot = TaskGroupDailyMessageSlot(
        id=slot_id,
        tenant_id=TENANT_ID,
        task_id=task_id,
        task_day_ledger_id=ledger.id,
        target_operation_target_id=TARGET_ID,
        slot_kind="quantity",
        slot_ordinal=1,
        pacing_contract_version=PACING_CONTRACT_VERSION,
        pacing_plan_hash="a" * 64,
        pacing_slot_ordinal=1,
        pacing_plan_total=100,
        pacing_due_at=due_at,
        release_not_before_at=due_at,
    )
    return task, ledger, slot


def _action(action_id: str, task_id: str, slot_id: str, due_at: datetime) -> Action:
    return Action(
        id=action_id,
        tenant_id=TENANT_ID,
        task_id=task_id,
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=ACCOUNT_ID,
        status="claiming",
        primary_quantity_slot_id=slot_id,
        pacing_contract_version=PACING_CONTRACT_VERSION,
        pacing_plan_hash="a" * 64,
        pacing_slot_key=f"slot:{slot_id}",
        pacing_due_at=due_at,
        release_not_before_at=due_at,
        effective_claim_at=due_at,
        payload={
            "group_id": TARGET_ID,
            "message_text": "test",
            "target_reference_snapshot": {"tg_peer_id": "-1009001"},
        },
    )


def _attempt(action: Action, attempt_no: int, timestamp: datetime) -> ExecutionAttempt:
    return ExecutionAttempt(
        tenant_id=TENANT_ID,
        action_id=action.id,
        account_id=ACCOUNT_ID,
        attempt_no=attempt_no,
        status="before_call",
        before_call_at=timestamp,
    )
