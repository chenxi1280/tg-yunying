from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

import pytest

from app.database import Base
from app.models import (
    Action,
    DispatchClaimReservation,
    DispatchClaimScope,
    DispatchClaimShardAllocation,
    DispatchClaimWindow,
    OperationTarget,
    Task,
    Tenant,
    TgAccount,
    TgGroup,
    TgGroupAccount,
)
from app.services._common import _now
from app.services.task_center import dispatcher, service
from app.services.task_center.payloads import SendMessagePayload


pytestmark = pytest.mark.no_postgres


def test_gateway_b1_failure_rolls_back_claim_and_action_together(
    monkeypatch,
) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _seed_active_claim(engine)

    def fail_after_claim_release(*_args, **_kwargs):
        raise RuntimeError("fault_after_claim_release")

    monkeypatch.setattr(
        dispatcher,
        "_sync_action_content_mix_state",
        fail_after_claim_release,
    )
    with Session(engine, autoflush=False) as session:
        action = session.get(Action, "action-1")
        action.status = "success"
        action.executed_at = _now()
        with pytest.raises(RuntimeError, match="fault_after_claim_release"):
            dispatcher._finalize_dispatch_action(session, action)
        session.rollback()

    with Session(engine) as session:
        action = session.get(Action, "action-1")
        scope = session.get(DispatchClaimScope, "scope-1")
        window = session.get(DispatchClaimWindow, "window-1")
        allocation = session.get(DispatchClaimShardAllocation, "allocation-1")
        assert action.status == "executing"
        assert action.result["dispatch_claim_active"] is True
        assert (scope.active_claim_count, window.active_claim_count) == (1, 1)
        assert allocation.active_claim_count == 1


def test_transaction_c_failure_does_not_roll_back_business_result(
    monkeypatch,
) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _seed_executing_action(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def complete_action(_session, action, **kwargs):
        assert kwargs["project_task_stats"] is False
        action.status = "success"
        action.executed_at = _now()
        return True

    def fail_projection(*_args, **_kwargs):
        raise OperationalError("projection", {}, RuntimeError("down"))

    monkeypatch.setattr(service, "dispatch_action", complete_action)
    monkeypatch.setattr(service, "project_dispatch_action_stats", fail_projection)

    assert service._dispatch_claimed_action_once(sessions, "action-c") == 1
    with sessions() as session:
        assert session.get(Action, "action-c").status == "success"


def test_pre_gateway_failure_waits_for_atomic_claim_release(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _seed_active_claim(engine)
    with Session(engine, autoflush=False) as session:
        action, context = _missing_group_gateway_context(session)
        committed_states: list[tuple[str, bool]] = []
        real_commit = session.commit

        def record_commit() -> None:
            committed_states.append(
                (action.status, bool((action.result or {}).get("dispatch_claim_active")))
            )
            real_commit()

        monkeypatch.setattr(session, "commit", record_commit)
        assert dispatcher._reserve_group_send_attempt(session, action, context) is None
        assert committed_states == []

        dispatcher._finalize_dispatch_action(
            session,
            action,
            ensure_remote_case=False,
            project_task_stats=False,
        )
        real_commit()

        assert session.get(Action, action.id).status == "failed"
        assert session.get(Action, action.id).result["dispatch_claim_active"] is False
        assert session.get(DispatchClaimScope, "scope-1").active_claim_count == 0


def _seed_active_claim(engine) -> None:
    observed_at = _now()
    with Session(engine) as session:
        session.add(Tenant(id=1, name="tenant"))
        session.add(Task(
            id="task-1",
            tenant_id=1,
            name="gateway",
            type="channel_view",
            status="running",
        ))
        scope = DispatchClaimScope(
            id="scope-1",
            dispatcher_scope="task_center_dispatch",
            claim_capacity=26,
            active_claim_count=1,
        )
        window = DispatchClaimWindow(
            id="window-1",
            dispatcher_scope="task_center_dispatch",
            bucket_start=observed_at - timedelta(seconds=10),
            bucket_end=observed_at + timedelta(seconds=50),
            claim_capacity=26,
            active_claim_count=1,
            effective_unclaimed_count=0,
        )
        allocation = DispatchClaimShardAllocation(
            id="allocation-1",
            dispatch_claim_window_id=window.id,
            dispatch_allocation_epoch=1,
            dispatch_contract_version="dispatch-rebuild-v3",
            account_shard_total=2,
            account_shard_index=0,
            active_claim_count=1,
        )
        reservation = DispatchClaimReservation(
            id="reservation-1",
            dispatch_claim_shard_allocation_id=allocation.id,
            dispatch_allocation_epoch=1,
            tenant_id=1,
            task_id="task-1",
            claim_class="ordinary",
            bucket_start=window.bucket_start,
            required_claims=1,
            reserved_claims=1,
            claimed_count=1,
        )
        action = Action(
            id="action-1",
            tenant_id=1,
            task_id="task-1",
            task_type="channel_view",
            action_type="noop_remote_probe",
            status="executing",
            scheduled_at=observed_at - timedelta(minutes=1),
            payload={},
            result={
                "dispatch_claim_active": True,
                "dispatch_claim_scope": scope.dispatcher_scope,
                "dispatch_claim_window_id": window.id,
                "dispatch_claim_shard_allocation_id": allocation.id,
                "dispatch_reservation_id": reservation.id,
            },
        )
        session.add_all([scope, window, allocation, reservation, action])
        session.commit()


def _missing_group_gateway_context(
    session: Session,
) -> tuple[Action, dispatcher.GroupSendGatewayContext]:
    action = session.get(Action, "action-1")
    task = session.get(Task, "task-1")
    account = TgAccount(
        id=1,
        tenant_id=1,
        display_name="sender",
        phone_masked="+861***0001",
        status="在线",
    )
    target = OperationTarget(
        id=404,
        tenant_id=1,
        target_type="group",
        tg_peer_id="-100404",
        title="missing",
    )
    task.type = "group_ai_chat"
    action.task_type = "group_ai_chat"
    action.action_type = "send_message"
    action.account_id = account.id
    action.payload = {
        "group_id": 404,
        "target_operation_target_id": target.id,
        "target_reference_revision": 1,
        "target_reference_snapshot": {"tg_peer_id": target.tg_peer_id},
    }
    session.add_all([account, target])
    session.commit()
    group = TgGroup(id=404, tenant_id=1, tg_peer_id=target.tg_peer_id, title="missing")
    link = TgGroupAccount(tenant_id=1, group_id=group.id, account_id=account.id, can_send=True)
    payload = SendMessagePayload(group_id=group.id, message_text="hello")
    return action, dispatcher.GroupSendGatewayContext(account, object(), group, link, payload, "hello")


def _seed_executing_action(engine) -> None:
    with Session(engine) as session:
        session.add(Tenant(id=1, name="tenant"))
        session.add(Task(
            id="task-c",
            tenant_id=1,
            name="projection",
            type="channel_view",
            status="running",
        ))
        session.add(Action(
            id="action-c",
            tenant_id=1,
            task_id="task-c",
            task_type="channel_view",
            action_type="view_message",
            status="executing",
            scheduled_at=_now(),
            payload={},
            result={},
        ))
        session.commit()
