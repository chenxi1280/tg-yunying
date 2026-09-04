from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.models import ChannelMessage, Task, TaskDayLedger, ViewFulfillmentObligation
from app.services.task_center import channel_fulfillment
from app.services.task_center.executors import channel_view, channel_view_allocation
from app.services.task_center.executors import channel_view_pacing
from app.services.task_center.payloads import ViewMessagePayload


pytestmark = pytest.mark.no_postgres


def test_view_matching_never_exceeds_remaining_task_daily_budget(monkeypatch) -> None:
    now = datetime(2026, 8, 28, 12, 0)
    messages = [
        SimpleNamespace(id=1, published_at=now, created_at=now),
        SimpleNamespace(
            id=2,
            published_at=now - timedelta(minutes=1),
            created_at=now - timedelta(minutes=1),
        ),
    ]
    inputs = channel_view_allocation.ViewPlanInputs(
        messages=messages,
        accounts=[SimpleNamespace(id=account_id) for account_id in range(1, 7)],
        task_remaining_today=5,
        daily_counts_by_account={},
        targets_by_message={
            message.id: SimpleNamespace(ledger_confirmed_at_attach=0)
            for message in messages
        },
        ledger=object(),
        lifetime_ids_by_message={message.id: set() for message in messages},
        materialized_ids_by_message={message.id: set() for message in messages},
        allowed_account_ids_by_message=None,
        now=now,
    )
    monkeypatch.setattr(
        channel_view_allocation,
        "channel_view_target_due",
        lambda *_args, **_kwargs: 3,
    )

    actions = channel_view._view_actions_for_messages(
        SimpleNamespace(pacing_config={}),
        {"max_views_per_account_per_day": 10},
        inputs,
    )

    assert len(actions) == 5


def test_existing_view_obligation_must_match_action_identity(monkeypatch) -> None:
    session = MagicMock()
    session.scalar.return_value = None
    task = SimpleNamespace(id="task-2", tenant_id=1)
    message = SimpleNamespace(id=2, tenant_id=1)
    ledger = SimpleNamespace(id="ledger-1", tenant_id=1, task_id="task-1")
    obligation = SimpleNamespace(
        id="wrong-obligation",
        tenant_id=1,
        task_day_ledger_id=ledger.id,
        channel_message_id=1,
        account_id=1,
        current_action_id=None,
        status="open",
    )
    action = SimpleNamespace(
        id="action-2",
        tenant_id=1,
        task_id=task.id,
        account_id=2,
        obligation_id=obligation.id,
    )

    def get_model(model, identity):
        values = {
            (Task, task.id): task,
            (ChannelMessage, message.id): message,
            (TaskDayLedger, ledger.id): ledger,
            (ViewFulfillmentObligation, obligation.id): obligation,
        }
        return values.get((model, identity))

    session.get.side_effect = get_model
    with pytest.raises(ValueError, match="view_obligation_identity_mismatch"):
        channel_fulfillment.ensure_view_action_contract(
            session,
            action,
            ViewMessagePayload(
                channel_id="peer-2",
                channel_message_id=message.id,
                message_id=102,
                task_day_ledger_id="ledger-2",
                view_fulfillment_obligation_id=obligation.id,
            ),
            now=datetime(2026, 8, 28, 12, 0),
        )


def test_message_expiry_converts_aware_utc_to_beijing_wall(monkeypatch) -> None:
    monkeypatch.setattr(channel_view, "_now", lambda: datetime(2026, 8, 29, 0, 30))
    message = SimpleNamespace(
        published_at=datetime(2026, 8, 27, 16, 45, tzinfo=timezone.utc)
    )

    assert channel_view._message_expired(message, {"message_active_days": 1}) is False


def test_view_plan_item_normalizes_all_utc_storage_boundaries() -> None:
    target = SimpleNamespace(
        accrual_anchor_at=datetime(2026, 8, 28, 0, 45),
        active_until=datetime(2026, 8, 28, 16, 0),
        effective_target_snapshot=3,
    )
    ledger = SimpleNamespace(
        id="ledger-1",
        period_start_at=datetime(2026, 8, 27, 16, 0),
        planning_anchor_at=datetime(2026, 8, 27, 16, 30),
        deadline_at=datetime(2026, 8, 28, 16, 0),
    )
    context = channel_view_pacing.ViewCreationContext(
        channel=SimpleNamespace(tg_peer_id="-1001"),
        config={},
        execution_date="2026-08-28",
        ledger=ledger,
        targets_by_message={1: target},
    )
    obligation = SimpleNamespace(
        id="obligation-1",
        pacing_slot_ordinal=0,
        pacing_due_at=None,
        pacing_period_key=None,
        pacing_plan_total=None,
        release_not_before_at=None,
        source_capacity_plan_hash=None,
        source_capacity_slot_ordinal=None,
    )

    item = channel_view_pacing._view_plan_item(
        SimpleNamespace(id="task-1", task_lifecycle_epoch=1),
        context,
        SimpleNamespace(id=1),
        7,
        obligation,
    )

    assert item.source_slot.period_start_at == datetime(2026, 8, 28, 0, 45)
    assert item.source_slot.deadline_at == datetime(2026, 8, 29, 0, 0)
