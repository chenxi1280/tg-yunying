from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountPacingReservation,
    AppUser,
    ChannelMessage,
    ContentMixContract,
    ContentMixCycle,
    ContentMixCycleSlot,
    CommentFulfillmentObligation,
    Action,
    ExecutionAttempt,
    PendingVisibilityCredit,
    SearchClickFulfillmentObligation,
    ReactionFulfillmentObligation,
    ReactionRemoteFact,
    Task,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
    TaskStartOperation,
    Tenant,
    ViewFulfillmentObligation,
    ViewRemoteFact,
)
from app.services.task_center.channel_fulfillment import (
    cancel_superseded_channel_actions,
    confirm_reaction_obligation,
    confirm_view_obligation,
    ensure_reaction_action_contract,
    reaction_account_ids_for_messages,
    view_account_ids_for_messages,
    view_confirmed_counts,
    view_daily_counts,
)
from app.services.task_center.channel_payloads import LikeMessagePayload
from app.services.task_center.config_normalization import validated_type_config
from app.services.task_center.fulfillment_takeover import UNIFIED_TASK_GATE_LIMIT


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        current.add(Tenant(id=1, name="单用户"))
        current.add(
            AppUser(
                id=7,
                tenant_id=1,
                name="运营",
                role="admin",
                email="operator@example.com",
            )
        )
        current.flush()
        yield current


def _task(*, task_id: str, request_id: str) -> Task:
    return Task(
        id=task_id,
        tenant_id=1,
        name="AI 活群",
        type="group_ai_chat",
        created_by_user_id=7,
        create_task_type="group_ai_chat",
        client_request_id=request_id,
        request_fingerprint=f"fingerprint:{request_id}",
    )


def test_create_idempotency_key_is_not_released_by_soft_delete(session: Session) -> None:
    original = _task(task_id="task-1", request_id="request-1")
    original.deleted_at = datetime.now(timezone.utc)
    session.add(original)
    session.commit()

    session.add(_task(task_id="task-2", request_id="request-1"))

    with pytest.raises(IntegrityError):
        session.commit()


def test_ai_quantity_slot_can_belong_to_only_one_cycle_slot(session: Session) -> None:
    task = _task(task_id="task-cycle", request_id="request-cycle")
    ledger = TaskDayLedger(
        id="ledger-1",
        tenant_id=1,
        task_id=task.id,
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=date(2026, 7, 29),
        period_start_at=datetime(2026, 7, 28, 16, tzinfo=timezone.utc),
        deadline_at=datetime(2026, 7, 29, 16, tzinfo=timezone.utc),
        day_phase="full_day_committed",
        planning_anchor_at=datetime(2026, 7, 28, 16, tzinfo=timezone.utc),
    )
    quantity_slot = TaskGroupDailyMessageSlot(
        id="quantity-slot-1",
        tenant_id=1,
        task_id=task.id,
        task_day_ledger_id=ledger.id,
        target_operation_target_id=11,
        slot_kind="extra_volume",
        slot_ordinal=1,
    )
    session.add_all([task, ledger, quantity_slot])
    session.flush()

    for sequence in (1, 2):
        cycle = ContentMixCycle(
            id=f"cycle-{sequence}",
            tenant_id=1,
            task_id=task.id,
            target_operation_target_id=11,
            task_day_ledger_id=ledger.id,
            cycle_seq=sequence,
            config_revision=1,
            scope_total_slots=1,
            allocation_seed=f"seed-{sequence}",
            allocation_closed_at=datetime.now(timezone.utc),
        )
        contract = ContentMixContract(
            id=f"contract-{sequence}",
            tenant_id=1,
            content_mix_scope_key=f"ai:{task.id}:11:{cycle.id}:1",
            content_contract_version=1,
            scope_total_slots=1,
            allocation_seed=f"seed-{sequence}",
            reply_min_required_count=0,
            reply_planned_count=0,
            direct_planned_count=1,
        )
        session.add_all([cycle, contract])
        session.flush()
        session.add(
            ContentMixCycleSlot(
                id=f"cycle-slot-{sequence}",
                tenant_id=1,
                cycle_id=cycle.id,
                slot_index=1,
                primary_quantity_slot_id=quantity_slot.id,
                relation_kind="direct",
            )
        )
        if sequence == 1:
            session.commit()

    with pytest.raises(IntegrityError):
        session.commit()


def test_start_operation_is_current_state_not_attempt_history(session: Session) -> None:
    task = _task(task_id="task-start", request_id="request-start")
    session.add(task)
    session.flush()
    session.add_all(
        [
            TaskStartOperation(
                task_id=task.id,
                start_operation_id="start-1",
                operation_version=1,
                requested_by_user_id=7,
                source="explicit_start",
                status="failed",
            ),
            TaskStartOperation(
                task_id=task.id,
                start_operation_id="start-2",
                operation_version=2,
                requested_by_user_id=7,
                source="explicit_start",
                status="processing",
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        session.commit()


def test_non_ai_obligations_use_natural_keys_not_ai_quantity_slots(
    session: Session,
) -> None:
    assert not hasattr(CommentFulfillmentObligation, "primary_quantity_slot_id")
    assert not hasattr(SearchClickFulfillmentObligation, "primary_quantity_slot_id")

    task = _task(task_id="task-comment", request_id="request-comment")
    session.add(task)
    session.flush()
    session.add(
        CommentFulfillmentObligation(
            id="comment-1",
            tenant_id=1,
            task_id=task.id,
            channel_message_id=88,
            comment_plan_revision=1,
            target_ordinal=1,
            status="confirmed",
            telegram_discussion_peer_id="-10088",
            remote_comment_id="9001",
        )
    )
    session.commit()
    session.add(
        CommentFulfillmentObligation(
            id="comment-2",
            tenant_id=1,
            task_id=task.id,
            channel_message_id=88,
            comment_plan_revision=1,
            target_ordinal=2,
            status="confirmed",
            telegram_discussion_peer_id="-10088",
            remote_comment_id="9001",
        )
    )

    with pytest.raises(IntegrityError):
        session.commit()


def test_channel_task_soft_limits_are_system_owned_gates() -> None:
    view = validated_type_config(
        "channel_view",
        {
            "target_channel_id": 31,
            "task_daily_view_safety_cap": 500,
            "max_views_per_account_per_day": 50,
        },
    )
    like = validated_type_config(
        "channel_like",
        {
            "target_channel_id": 31,
            "max_likes_per_account_per_hour": 10,
        },
    )
    comment = validated_type_config(
        "channel_comment",
        {
            "target_channel_id": 31,
            "max_total_comments": 80,
            "max_comments_per_account_per_hour": 3,
        },
    )

    assert view["task_daily_view_safety_cap"] == UNIFIED_TASK_GATE_LIMIT
    assert view["max_views_per_account_per_day"] == UNIFIED_TASK_GATE_LIMIT
    assert like["max_likes_per_account_per_hour"] == UNIFIED_TASK_GATE_LIMIT
    assert comment["max_total_comments"] == UNIFIED_TASK_GATE_LIMIT
    assert comment["max_comments_per_account_per_hour"] == UNIFIED_TASK_GATE_LIMIT


def test_superseded_reaction_action_is_released_before_replanning(
    session: Session,
) -> None:
    task = _task(task_id="task-like-superseded", request_id="request-like-superseded")
    task.type = "channel_like"
    task.status = "running"
    task.task_lifecycle_epoch = 4
    action = Action(
        id="like-action-superseded",
        tenant_id=1,
        task_id=task.id,
        task_type="channel_like",
        action_type="like_message",
        account_id=101,
        status="pending",
        task_lifecycle_epoch=2,
        pacing_slot_key="like:task-like-superseded:88:101",
        payload={
            "reaction_fulfillment_obligation_id": "reaction-superseded",
        },
    )
    obligation = ReactionFulfillmentObligation(
        id="reaction-superseded",
        tenant_id=1,
        task_id=task.id,
        channel_message_id=88,
        account_id=101,
        reaction_contract_version=1,
        current_action_id=action.id,
        status="pending",
    )
    reservation = AccountPacingReservation(
        id="reaction-superseded-reservation",
        tenant_id=1,
        task_id=task.id,
        account_id=101,
        pacing_slot_key=action.pacing_slot_key,
        policy_version="test",
        due_at=datetime.now(timezone.utc),
        release_not_before_at=datetime.now(timezone.utc),
        effective_claim_at=datetime.now(timezone.utc),
        action_id=action.id,
        state="bound",
    )
    session.add_all([task, action, obligation, reservation])
    session.flush()

    assert cancel_superseded_channel_actions(session, task) == 1
    assert action.status == "skipped"
    assert action.result["error_code"] == "task_lifecycle_superseded_pre_gateway"
    assert obligation.status == "open"
    assert obligation.current_action_id is None
    assert reservation.state == "missed"


def test_superseded_reaction_action_with_gateway_evidence_is_not_cancelled(
    session: Session,
) -> None:
    task = _task(task_id="task-like-gateway", request_id="request-like-gateway")
    task.type = "channel_like"
    task.status = "running"
    task.task_lifecycle_epoch = 4
    action = Action(
        id="like-action-gateway",
        tenant_id=1,
        task_id=task.id,
        task_type="channel_like",
        action_type="like_message",
        account_id=101,
        status="pending",
        task_lifecycle_epoch=2,
    )
    session.add_all([
        task,
        action,
        ExecutionAttempt(
            tenant_id=1,
            action_id=action.id,
            attempt_no=1,
            status="executing",
            gateway_call_started_at=datetime.now(timezone.utc),
        ),
    ])
    session.flush()

    assert cancel_superseded_channel_actions(session, task) == 0
    assert action.status == "pending"


def test_reaction_and_view_remote_facts_confirm_once(
    session: Session,
) -> None:
    like_task = _task(task_id="task-like", request_id="request-like")
    like_task.type = "channel_like"
    view_task = _task(task_id="task-view", request_id="request-view")
    view_task.type = "channel_view"
    ledger = TaskDayLedger(
        id="ledger-view",
        tenant_id=1,
        task_id=view_task.id,
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=date(2026, 7, 29),
        period_start_at=datetime(2026, 7, 28, 16, tzinfo=timezone.utc),
        deadline_at=datetime(2026, 7, 29, 16, tzinfo=timezone.utc),
        day_phase="full_day_committed",
        planning_anchor_at=datetime(2026, 7, 28, 16, tzinfo=timezone.utc),
    )
    like_action = Action(
        id="like-action",
        tenant_id=1,
        task_id=like_task.id,
        task_type="channel_like",
        action_type="like_message",
        account_id=101,
    )
    view_action = Action(
        id="view-action",
        tenant_id=1,
        task_id=view_task.id,
        task_type="channel_view",
        action_type="view_message",
        account_id=101,
    )
    reaction = ReactionFulfillmentObligation(
        id="reaction-obligation",
        tenant_id=1,
        task_id=like_task.id,
        channel_message_id=88,
        account_id=101,
        reaction_contract_version=1,
        current_action_id=like_action.id,
        status="pending",
    )
    view = ViewFulfillmentObligation(
        id="view-obligation",
        tenant_id=1,
        task_day_ledger_id=ledger.id,
        channel_message_id=88,
        account_id=101,
        current_action_id=view_action.id,
        status="pending",
    )
    session.add_all(
        [like_task, view_task, ledger, like_action, view_action, reaction, view]
    )
    session.flush()
    messages = [ChannelMessage(id=88, tenant_id=1, channel_target_id=31, message_id=8)]
    other_like = _task(task_id="task-like-other", request_id="request-like-other")
    other_like.type = "channel_like"
    other_view = _task(task_id="task-view-other", request_id="request-view-other")
    other_view.type = "channel_view"
    duplicate_action = Action(
        id="like-action-other",
        tenant_id=1,
        task_id=other_like.id,
        task_type="channel_like",
        action_type="like_message",
        account_id=101,
    )
    session.add_all([other_like, other_view, duplicate_action])
    session.flush()

    assert reaction_account_ids_for_messages(
        session, other_like, messages
    ) == {88: {101}}
    assert view_account_ids_for_messages(
        session, other_view, ledger, messages
    ) == {88: {101}}
    with pytest.raises(ValueError, match="reaction_remote_source_held"):
        ensure_reaction_action_contract(
            session,
            duplicate_action,
            LikeMessagePayload(
                channel_id="-10031",
                channel_message_id=88,
                message_id=8,
            ),
        )

    for _ in range(2):
        confirm_reaction_obligation(
            session,
            reaction,
            target_peer_id="-10031",
            reaction_emoji="👍",
            confirmed_at=datetime.now(timezone.utc),
        )
        confirm_view_obligation(
            session,
            view,
            target_peer_id="-10031",
            confirmed_at=datetime.now(timezone.utc),
        )
    session.flush()

    assert reaction.status == "confirmed"
    assert view.status == "confirmed"
    assert session.query(ReactionRemoteFact).count() == 1
    assert session.query(ViewRemoteFact).count() == 1
    assert reaction_account_ids_for_messages(
        session,
        like_task,
        messages,
    ) == {88: {101}}
    assert view_account_ids_for_messages(
        session,
        view_task,
        ledger,
        messages,
    ) == {88: {101}}
    assert view_confirmed_counts(session, view_task, messages) == {88: 1}
    daily = view_daily_counts(session, ledger)
    assert daily.total == 1
    assert daily.by_account == {101: 1}
    other_ledger = TaskDayLedger(
        id="ledger-view-other",
        tenant_id=1,
        task_id=other_view.id,
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=date(2026, 7, 29),
        period_start_at=datetime(2026, 7, 28, 16, tzinfo=timezone.utc),
        deadline_at=datetime(2026, 7, 29, 16, tzinfo=timezone.utc),
        day_phase="full_day_committed",
        planning_anchor_at=datetime(2026, 7, 28, 16, tzinfo=timezone.utc),
    )
    session.add(other_ledger)
    session.flush()

    assert reaction_account_ids_for_messages(
        session,
        other_like,
        messages,
    ) == {88: {101}}
    assert view_account_ids_for_messages(
        session,
        other_view,
        other_ledger,
        messages,
    ) == {88: {101}}


def test_pending_visibility_holds_one_ai_slot_without_confirming_it(
    session: Session,
) -> None:
    task = _task(task_id="task-hold", request_id="request-hold")
    ledger = TaskDayLedger(
        id="ledger-hold",
        tenant_id=1,
        task_id=task.id,
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=date(2026, 7, 29),
        period_start_at=datetime(2026, 7, 28, 16, tzinfo=timezone.utc),
        deadline_at=datetime(2026, 7, 29, 16, tzinfo=timezone.utc),
        day_phase="full_day_committed",
        planning_anchor_at=datetime(2026, 7, 28, 16, tzinfo=timezone.utc),
    )
    quantity_slot = TaskGroupDailyMessageSlot(
        id="quantity-hold",
        tenant_id=1,
        task_id=task.id,
        task_day_ledger_id=ledger.id,
        target_operation_target_id=11,
        slot_kind="extra_volume",
        slot_ordinal=1,
    )
    actions = [
        Action(
            id=f"hold-action-{index}",
            tenant_id=1,
            task_id=task.id,
            task_type="group_ai_chat",
            action_type="send_message",
        )
        for index in (1, 2)
    ]
    session.add_all([task, ledger, quantity_slot, *actions])
    session.flush()
    session.add(
        PendingVisibilityCredit(
            tenant_id=1,
            action_id=actions[0].id,
            task_day_ledger_id=ledger.id,
            primary_quantity_slot_id=quantity_slot.id,
            remote_message_id="remote-1",
            admission_version=3,
            status="open",
        )
    )
    session.commit()
    session.add(
        PendingVisibilityCredit(
            tenant_id=1,
            action_id=actions[1].id,
            task_day_ledger_id=ledger.id,
            primary_quantity_slot_id=quantity_slot.id,
            remote_message_id="remote-2",
            admission_version=3,
            status="unknown",
        )
    )

    with pytest.raises(IntegrityError):
        session.commit()
