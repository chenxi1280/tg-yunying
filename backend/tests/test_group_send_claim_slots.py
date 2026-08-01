from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, Task, Tenant, TgAccount, TgGroup, TgGroupAccount
from app.services.task_center.group_send_claim_slots import (
    filter_ready_group_send_actions,
    lock_eligible_group_send_actions,
)
from app.services.task_center.group_send_limits import (
    SEND_LIMIT_MODE_ACCOUNT_ONLY,
    group_send_slot_block,
    reserve_group_send_slot,
    settle_group_send_slot,
)
from app.services.task_center import dispatcher


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _pending_action(action_id: str, group_id: int, account_id: int, now_value: datetime) -> Action:
    return Action(
        id=action_id,
        tenant_id=1,
        task_id="task-ai",
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=account_id,
        status="pending",
        scheduled_at=now_value,
        payload={
            "content_scope_contract_version": "group_content_scope_v1",
            "group_id": group_id,
            "message_text": action_id,
        },
    )


def _seed_groups(session: Session, now_value: datetime) -> list[Action]:
    session.add_all(
        [
            Tenant(id=1, name="t"),
            Task(id="task-ai", tenant_id=1, name="ai", type="group_ai_chat", status="running"),
            TgGroup(
                id=7,
                tenant_id=1,
                tg_peer_id="-1007",
                title="legacy",
                group_type="supergroup",
                group_cooldown_seconds=15,
            ),
            TgGroup(
                id=8,
                tenant_id=1,
                tg_peer_id="-1008",
                title="account only",
                group_type="supergroup",
                send_limit_mode=SEND_LIMIT_MODE_ACCOUNT_ONLY,
            ),
            TgAccount(id=11, tenant_id=1, display_name="first", phone_masked="+101", status="在线"),
            TgAccount(id=12, tenant_id=1, display_name="second", phone_masked="+102", status="在线"),
            TgAccount(id=13, tenant_id=1, display_name="third", phone_masked="+103", status="在线"),
        ]
    )
    actions = [
        _pending_action("legacy-first", 7, 11, now_value),
        _pending_action("legacy-second", 7, 12, now_value),
        _pending_action("account-only", 8, 13, now_value),
    ]
    session.add_all(
        [
            *actions,
            TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True),
            TgGroupAccount(tenant_id=1, group_id=7, account_id=12, can_send=True),
            TgGroupAccount(tenant_id=1, group_id=8, account_id=13, can_send=True),
        ]
    )
    session.flush()
    return actions


def test_ai_actions_ignore_future_legacy_group_slot() -> None:
    now_value = datetime(2026, 7, 27, 12, 0)
    with _session() as session:
        actions = _seed_groups(session, now_value)
        session.get(TgGroup, 7).next_group_send_slot_at = now_value + timedelta(seconds=15)

        selected = filter_ready_group_send_actions(session, actions, now_value)

        assert [action.id for action in selected] == ["legacy-first", "legacy-second", "account-only"]


def test_ai_actions_ignore_legacy_group_claim_lock() -> None:
    now_value = datetime(2026, 7, 27, 12, 0)
    with _session() as session:
        actions = _seed_groups(session, now_value)

        selected = lock_eligible_group_send_actions(session, actions, now_value)

        assert [action.id for action in selected] == ["legacy-first", "legacy-second", "account-only"]


def test_ai_actions_ignore_inflight_legacy_group_slot() -> None:
    now_value = datetime(2026, 7, 27, 12, 0)
    with _session() as session:
        actions = _seed_groups(session, now_value)
        session.add(
            Action(
                id="legacy-inflight",
                tenant_id=1,
                task_id="task-ai",
                task_type="group_ai_chat",
                action_type="send_message",
                status="executing",
                payload={"group_id": 7, "message_text": "inflight"},
            )
        )
        session.flush()

        selected = lock_eligible_group_send_actions(session, actions, now_value)

        assert [action.id for action in selected] == ["legacy-first", "legacy-second", "account-only"]


def test_dispatcher_claims_all_due_ai_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    now_value = datetime(2026, 7, 27, 12, 0)
    monkeypatch.setattr(dispatcher, "_now", lambda: now_value)
    monkeypatch.setattr(
        dispatcher,
        "get_settings",
        lambda: SimpleNamespace(
            enable_redis_token_bucket=False,
            action_claim_limit=10,
            action_claim_seconds=60,
            action_lease_seconds=1800,
            dispatcher_concurrency=10,
            account_shard_total=1,
            account_shard_index=0,
            enable_redis_account_inflight=False,
            redis_account_inflight_seconds=1800,
        ),
    )
    with _session() as session:
        _seed_groups(session, now_value)
        session.commit()

        claimed = dispatcher.claim_actions(session, limit=3, worker_id="group-slot-test")

        assert [action.id for action in claimed] == ["legacy-first", "legacy-second", "account-only"]
        assert session.get(Action, "legacy-second").status == "executing"


def test_gateway_slot_reservation_advances_only_legacy_group() -> None:
    now_value = datetime(2026, 7, 27, 12, 0)
    with _session() as session:
        _seed_groups(session, now_value)
        legacy = session.get(TgGroup, 7)
        account_only = session.get(TgGroup, 8)

        reserve_group_send_slot(legacy, now_value)
        reserve_group_send_slot(account_only, now_value)

        assert legacy.next_group_send_slot_at == now_value + timedelta(seconds=15)
        assert account_only.next_group_send_slot_at is None


def test_ai_gateway_ignores_persisted_legacy_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.task_center import group_send_limits

    now_value = datetime(2026, 7, 27, 12, 0)
    monkeypatch.setattr(group_send_limits, "_now", lambda: now_value)
    with _session() as session:
        actions = _seed_groups(session, now_value)
        group = session.get(TgGroup, 7)
        group.next_group_send_slot_at = now_value + timedelta(seconds=15)

        block = group_send_slot_block(session, action=actions[0], group=group)

        assert block is None


def test_known_gateway_failure_releases_only_its_slot_reservation(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.task_center import group_send_limits

    now_value = datetime(2026, 7, 27, 12, 0)
    monkeypatch.setattr(group_send_limits, "_now", lambda: now_value)
    with _session() as session:
        _seed_groups(session, now_value)
        group = session.get(TgGroup, 7)

        reserved_until = reserve_group_send_slot(group)
        assert reserved_until == now_value + timedelta(seconds=15)
        assert settle_group_send_slot(group, reserved_until=reserved_until)
        assert group.next_group_send_slot_at is None

        newer_reservation = reserve_group_send_slot(group)
        group.next_group_send_slot_at = newer_reservation + timedelta(seconds=15)

        assert not settle_group_send_slot(group, reserved_until=newer_reservation)
        assert group.next_group_send_slot_at == now_value + timedelta(seconds=30)


def test_rate_limited_gateway_failure_uses_explicit_retry_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.task_center import group_send_limits

    now_value = datetime(2026, 7, 27, 12, 0)
    monkeypatch.setattr(group_send_limits, "_now", lambda: now_value)
    with _session() as session:
        _seed_groups(session, now_value)
        group = session.get(TgGroup, 7)

        reserved_until = reserve_group_send_slot(group)

        assert settle_group_send_slot(group, reserved_until=reserved_until, retry_after_seconds=120)
        assert group.next_group_send_slot_at == now_value + timedelta(seconds=120)
