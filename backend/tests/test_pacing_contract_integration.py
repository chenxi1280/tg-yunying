from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AccountPacingReservation,
    Action,
    FulfillmentRemoteFact,
    GenerationJob,
    Task,
    TgAccount,
)
from app.services.task_center.account_pacing_guard import (
    bind_account_pacing_reservation,
    reserve_account_pacing,
)
from app.services.task_center.direct_action_claims import claim_fact_first_candidates
from app.services.task_center import account_pacing_guard
from app.services.task_center import dispatcher
from app.services.task_center.source_pacing import (
    SourcePacingSlot,
    schedule_source_pacing_points,
    schedule_source_pacing_slots,
)
from app.services.task_center.pacing_quantity import deterministic_quantity_with_jitter
from pacing_contract_test_support import flat_curve as _curve
from pacing_contract_test_support import pacing_engine as _pacing_engine


pytestmark = pytest.mark.no_postgres


@dataclass(frozen=True)
class ClaimCaseSpec:
    slot_key: str
    deadline_at: datetime
    action_id: str
    conflict_id: str


def _prepare_claim_case(
    session: Session,
    now: datetime,
    spec: ClaimCaseSpec,
) -> tuple[Task, AccountPacingReservation, Action]:
    task = session.get(Task, "pacing-task")
    task.status = "running"
    task.fulfillment_contract_version = "fact_first_v3"
    reservation = reserve_account_pacing(
        session,
        tenant_id=1,
        task_id=task.id,
        account_id=9101,
        slot_key=spec.slot_key,
        due_at=now,
        deadline_at=spec.deadline_at,
    )
    current = Action(
        id=spec.action_id,
        tenant_id=1,
        task_id=task.id,
        task_type="channel_like",
        action_type="like_message",
        account_id=9101,
        status="pending",
        scheduled_at=now,
        pacing_slot_key=spec.slot_key,
        pacing_due_at=now,
        release_not_before_at=now,
        effective_claim_at=now,
    )
    session.add(current)
    session.flush()
    bind_account_pacing_reservation(reservation, current)
    session.add(Action(
        id=spec.conflict_id,
        tenant_id=1,
        task_id=task.id,
        task_type="channel_like",
        action_type="like_message",
        account_id=9101,
        status="pending",
        scheduled_at=now,
    ))
    session.commit()
    return task, reservation, current


def _prepare_speaker_rebind_case(session: Session) -> tuple[Action, GenerationJob]:
    session.add(TgAccount(
        id=9102,
        tenant_id=1,
        display_name="replacement account",
        phone_masked="+86****9102",
    ))
    action = Action(
        id="legacy-comment-rebind",
        tenant_id=1,
        task_id="pacing-task",
        task_type="channel_comment",
        action_type="post_comment",
        account_id=9101,
        status="executing",
        payload={
            "comment_fulfillment_obligation_id": "comment-obligation",
            "comment_text": "old account candidate",
            "ai_generation_status": "ready",
        },
        candidate_hash="old-candidate",
    )
    job = GenerationJob(
        id="comment-job",
        tenant_id=1,
        task_id="pacing-task",
        obligation_type="post_comment",
        obligation_id="comment-obligation",
        generation_sequence=1,
        context_snapshot_version=1,
        state="pending",
    )
    session.add_all([action, job])
    session.flush()
    return action, job


def test_source_schedule_keeps_fresh_source_when_other_source_expired() -> None:
    now = datetime(2026, 8, 16, 10, 0)
    slots = [
        SourcePacingSlot("old", "old:0", 0, 1, now - timedelta(days=2), now - timedelta(days=1)),
        SourcePacingSlot("fresh", "fresh:0", 0, 2, now, now + timedelta(days=1)),
        SourcePacingSlot("fresh", "fresh:1", 1, 2, now, now + timedelta(days=1)),
    ]

    planned = schedule_source_pacing_slots(
        slots,
        _curve(),
        seed_id="mixed-source",
        now_at=now,
    )

    assert "old:0" not in planned
    assert set(planned) == {"fresh:0", "fresh:1"}


def test_source_schedule_remaining_subset_reuses_frozen_period_slots() -> None:
    start = datetime(2026, 8, 16, 10, 0)
    deadline = start + timedelta(hours=4)
    full_slots = [
        SourcePacingSlot("message-1", f"slot:{index}", index, 4, start, deadline)
        for index in range(4)
    ]
    full = schedule_source_pacing_slots(full_slots, _curve(), seed_id="stable", now_at=start)
    subset = schedule_source_pacing_slots(
        full_slots[2:],
        _curve(),
        seed_id="stable",
        now_at=start + timedelta(minutes=37),
    )

    assert subset == {key: full[key] for key in ("slot:2", "slot:3")}


def test_overdue_source_slots_get_cross_account_recovery_releases() -> None:
    now = datetime(2026, 8, 16, 11, 0)
    period_start = now - timedelta(hours=2)
    deadline = now + timedelta(hours=2)
    slots = [
        SourcePacingSlot(
            "message-1", f"overdue:{index}", index, 8,
            period_start, deadline,
        )
        for index in range(3)
    ]

    points = schedule_source_pacing_points(
        slots, _curve(), seed_id="recovery", now_at=now,
    )
    releases = sorted(point.release_not_before_at for point in points.values())

    assert len(releases) == 3
    assert all(release > now for release in releases)
    assert len({release.replace(microsecond=0) for release in releases}) == 3
    assert all(
        right - left >= timedelta(minutes=30)
        for left, right in zip(releases, releases[1:])
    )


def test_twenty_minute_worker_pause_does_not_drain_overdue_within_one_minute() -> None:
    period_start = datetime(2026, 8, 16, 10, 0)
    now = period_start + timedelta(minutes=20)
    deadline = period_start + timedelta(hours=1)
    slots = [
        SourcePacingSlot(
            "hourly-source", f"paused:{index}", index, 20,
            period_start, deadline,
        )
        for index in range(6)
    ]

    points = schedule_source_pacing_points(
        slots, _curve(), seed_id="twenty-minute-pause", now_at=now,
    )
    releases = sorted(point.release_not_before_at for point in points.values())

    assert len(releases) == 6
    assert len({release.replace(second=0, microsecond=0) for release in releases}) == 6
    assert releases[-1] - releases[0] > timedelta(minutes=1)


def test_overdue_recovery_release_is_frozen_on_rerun() -> None:
    now = datetime(2026, 8, 16, 11, 0)
    period_start = now - timedelta(hours=1)
    deadline = now + timedelta(hours=2)
    slots = [
        SourcePacingSlot("message-1", f"slot:{index}", index, 4, period_start, deadline)
        for index in range(2)
    ]
    first = schedule_source_pacing_points(
        slots, _curve(), seed_id="frozen-recovery", now_at=now,
    )
    frozen_slots = [
        SourcePacingSlot(
            slot.source_key,
            slot.slot_key,
            slot.slot_ordinal,
            slot.plan_total,
            slot.period_start_at,
            slot.deadline_at,
            first[slot.slot_key].release_not_before_at,
        )
        for slot in slots
    ]

    second = schedule_source_pacing_points(
        frozen_slots,
        _curve(),
        seed_id="frozen-recovery",
        now_at=now + timedelta(minutes=5),
    )

    assert second == first


def test_quantity_jitter_is_stable_for_task_and_source() -> None:
    first = deterministic_quantity_with_jitter(20, 0.3, seed_id="like:task:message")
    second = deterministic_quantity_with_jitter(20, 0.3, seed_id="like:task:message")

    assert first == second
    assert 14 <= first <= 26


def test_account_pacing_reservation_persists_distinct_effective_claims(monkeypatch) -> None:
    monkeypatch.setattr(
        account_pacing_guard,
        "get_settings",
        lambda: SimpleNamespace(account_soft_pacing_min_gap_seconds=20),
    )
    engine = _pacing_engine()
    due = datetime(2026, 8, 16, 10, 0)
    deadline = due + timedelta(hours=2)
    with Session(engine) as session:
        first = reserve_account_pacing(
            session,
            tenant_id=1,
            task_id="pacing-task",
            account_id=9101,
            slot_key="like:1",
            due_at=due,
            deadline_at=deadline,
        )
        second = reserve_account_pacing(
            session,
            tenant_id=1,
            task_id="pacing-task",
            account_id=9101,
            slot_key="like:2",
            due_at=due,
            deadline_at=deadline,
        )
        session.commit()

        assert second.effective_claim_at > first.effective_claim_at
        assert second.effective_claim_at - first.effective_claim_at < timedelta(minutes=5)
        assert session.query(AccountPacingReservation).count() == 2


def test_direct_claim_revalidates_account_timeline_and_defers_conflict(monkeypatch) -> None:
    monkeypatch.setattr(
        account_pacing_guard,
        "get_settings",
        lambda: SimpleNamespace(account_soft_pacing_min_gap_seconds=20),
    )
    engine = _pacing_engine()
    now = datetime(2026, 8, 16, 10, 0)
    with Session(engine) as session:
        _task, reservation, current = _prepare_claim_case(
            session,
            now,
            ClaimCaseSpec(
                slot_key="claim:current",
                deadline_at=now + timedelta(hours=1),
                action_id="claim-current",
                conflict_id="claim-new-conflict",
            ),
        )

        batch = claim_fact_first_candidates(
            session,
            owner="claim-worker",
            limit=1,
            now=now,
            lease_seconds=30,
        )
        session.refresh(current)
        session.refresh(reservation)

        assert batch.action_ids == ()
        assert current.status == "pending"
        assert current.scheduled_at == now + timedelta(seconds=20)
        assert reservation.effective_claim_at == current.scheduled_at


def test_direct_claim_records_safe_shortfall_when_conflict_crosses_deadline(monkeypatch) -> None:
    monkeypatch.setattr(
        account_pacing_guard,
        "get_settings",
        lambda: SimpleNamespace(account_soft_pacing_min_gap_seconds=20),
    )
    engine = _pacing_engine()
    now = datetime(2026, 8, 16, 10, 0)
    with Session(engine) as session:
        task, reservation, current = _prepare_claim_case(
            session,
            now,
            ClaimCaseSpec(
                slot_key="claim:deadline",
                deadline_at=now + timedelta(seconds=10),
                action_id="claim-deadline-current",
                conflict_id="zz-claim-deadline-conflict",
            ),
        )

        batch = claim_fact_first_candidates(
            session, owner="claim-worker", limit=1, now=now, lease_seconds=30,
        )
        session.refresh(current)
        session.refresh(reservation)
        fact = session.scalar(select(FulfillmentRemoteFact).where(
            FulfillmentRemoteFact.action_id == current.id,
        ))

        assert batch.action_ids == ()
        assert current.status == "skipped"
        assert current.result["error_code"] == "pacing_claim_deadline_exceeded"
        assert reservation.state == "missed"
        assert fact is not None and fact.fact_kind == "safely_not_executed"
        with pytest.raises(account_pacing_guard.AccountPacingDeadlineExceeded):
            reserve_account_pacing(
                session,
                tenant_id=1,
                task_id=task.id,
                account_id=9101,
                slot_key="claim:deadline",
                due_at=now,
                deadline_at=now + timedelta(seconds=10),
            )


def test_bound_comment_cannot_use_generic_account_reassignment() -> None:
    action = Action(
        id="bound-comment",
        tenant_id=1,
        task_id="comment-task",
        task_type="channel_comment",
        action_type="post_comment",
        account_id=9101,
        payload={"comment_fulfillment_obligation_id": "comment-obligation"},
    )

    assert dispatcher._action_can_reassign(action) is False


def test_speaker_rebind_invalidates_old_generation_before_requeue(monkeypatch) -> None:
    engine = _pacing_engine()
    monkeypatch.setattr(dispatcher, "_release_runtime_resources", lambda _action: None)
    with Session(engine) as session:
        action, job = _prepare_speaker_rebind_case(session)

        dispatcher._requeue_comment_speaker_rebind(
            session,
            action,
            SimpleNamespace(comment_fulfillment_obligation_id="comment-obligation"),
            previous_account_id=9101,
            next_account_id=9102,
            reason="avoid_repeat_speaker",
            conversation_key="discussion:1",
        )
        session.flush()
        session.expire(job)

        assert action.status == "pending"
        assert action.account_id == 9102
        assert action.assignment_revision == 2
        assert action.intent_revision == 2
        assert action.candidate_hash == ""
        assert "comment_text" not in action.payload
        assert action.payload["ai_generation_status"] == "pending"
        assert job.state == "failed"
