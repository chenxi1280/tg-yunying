from __future__ import annotations

from datetime import datetime, timedelta

from app.models import (
    AccountPacingReservation,
    Action,
    ChannelViewDailyIdentityOwner,
    ExecutionAttempt,
    Task,
    TaskDayLedger,
    ViewFulfillmentObligation,
)


NOW = datetime(2026, 8, 18, 10, 0)
DEADLINE = NOW + timedelta(days=1)
TARGET_PEER_ID = "-1009001"


def daily_identity_owner(
    task: Task,
    ledger: TaskDayLedger,
    obligation: ViewFulfillmentObligation,
    action: Action,
) -> ChannelViewDailyIdentityOwner:
    return ChannelViewDailyIdentityOwner(
        tenant_id=task.tenant_id,
        target_peer_id=TARGET_PEER_ID,
        channel_message_id=obligation.channel_message_id,
        account_id=action.account_id,
        obligation_local_date=ledger.obligation_local_date,
        state="pre_gateway",
        logical_task_id=task.id,
        obligation_id=obligation.id,
        action_id=action.id,
        request_identity=f"{task.id}:{obligation.id}",
    )


def execution_attempt(action: Action, now: datetime) -> ExecutionAttempt:
    return ExecutionAttempt(
        tenant_id=1,
        action_id=action.id,
        account_id=1,
        attempt_no=1,
        status="before_call",
        before_call_at=now,
    )


def account_reservation(
    task: Task,
    action: Action,
    *,
    deadline: datetime,
    future: datetime,
) -> AccountPacingReservation:
    return AccountPacingReservation(
        tenant_id=1,
        task_id=task.id,
        account_id=1,
        pacing_slot_key=action.pacing_slot_key,
        policy_version="account_soft_pacing_v1",
        due_at=NOW,
        release_not_before_at=future,
        effective_claim_at=future,
        source_deadline_at=deadline,
        action_id=action.id,
        state="bound",
    )
