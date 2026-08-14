from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    AuditLog,
    ExecutionAttempt,
    OperationTarget,
    Task,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
    TaskGroupDailyTarget,
    Tenant,
    TgGroup,
)
from app.models.enums import FailureType
from app.services.task_center.ledger_route_repair import (
    apply_group_ai_ledger_route_repair,
    group_ai_ledger_route_repair_hash,
    preview_group_ai_ledger_route_repair,
)


pytestmark = pytest.mark.no_postgres


def test_group_ai_ledger_route_repair_restores_only_ledger_route() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _seed_route_drift(session)
        preview = preview_group_ai_ledger_route_repair(
            session,
            task_id="route-repair-task",
            ledger_id="route-repair-ledger",
        )
        manifest_hash = group_ai_ledger_route_repair_hash(preview)

        with pytest.raises(ValueError, match="repair_manifest_hash_mismatch"):
            apply_group_ai_ledger_route_repair(
                session,
                task_id="route-repair-task",
                ledger_id="route-repair-ledger",
                expected_manifest_hash="stale",
                approval_ref="INC-1",
                actor="test",
            )

        result = apply_group_ai_ledger_route_repair(
            session,
            task_id="route-repair-task",
            ledger_id="route-repair-ledger",
            expected_manifest_hash=manifest_hash,
            approval_ref="INC-1",
            actor="test",
        )
        task = session.get(Task, "route-repair-task")

        assert result["restored_group_id"] == 21
        assert result["restored_operation_target_id"] == 31
        assert task.type_config["target_group_id"] == 21
        assert task.type_config["target_operation_target_id"] == 31
        assert session.scalar(select(AuditLog.action)) == "repair_group_ai_ledger_route"


def test_group_ai_ledger_route_repair_cancels_only_unclaimed_wrong_route_actions() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _seed_route_drift(session)
        _seed_safe_route_actions(session)
        preview = preview_group_ai_ledger_route_repair(
            session,
            task_id="route-repair-task",
            ledger_id="route-repair-ledger",
        )
        assert preview["current_route_action_disposition"] == {
            "safe_terminal_actions": [
                {"id": "peer-invalid-route-action", "action_version": 1},
                {"id": "skipped-route-action", "action_version": 1},
            ],
            "pending_cancellation_actions": [
                {"id": "pending-route-action", "action_version": 1},
            ],
        }
        manifest_hash = group_ai_ledger_route_repair_hash(preview)

        result = apply_group_ai_ledger_route_repair(
            session,
            task_id="route-repair-task",
            ledger_id="route-repair-ledger",
            expected_manifest_hash=manifest_hash,
            approval_ref="INC-2",
            actor="test",
        )

        terminal = session.get(Action, "peer-invalid-route-action")
        skipped = session.get(Action, "skipped-route-action")
        pending = session.get(Action, "pending-route-action")
        assert result["cancelled_pending_action_ids"] == ["pending-route-action"]
        assert terminal.status == "closed_unknown"
        assert skipped.status == "skipped"
        assert pending.status == "cancelled"
        assert pending.action_version == 2
        assert pending.result["route_repair_disposition"] == "cancelled_before_ledger_route_restore"


def test_group_ai_ledger_route_repair_rejects_actions_without_no_mutation_proof() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _seed_route_drift(session)
        pending = _route_action("unsafe-pending-route-action", status="pending")
        unknown = _route_action(
            "unsafe-unknown-route-action",
            status="closed_unknown",
            result={"error_code": FailureType.UNKNOWN.value},
        )
        session.add_all([
            pending,
            unknown,
            ExecutionAttempt(
                id="unsafe-pending-attempt",
                tenant_id=1,
                action_id=pending.id,
                attempt_no=1,
                status="before_call",
            ),
        ])
        session.commit()

        with pytest.raises(ValueError, match="current_route_actions_unsafe"):
            preview_group_ai_ledger_route_repair(
                session,
                task_id="route-repair-task",
                ledger_id="route-repair-ledger",
            )


def _seed_route_drift(session: Session) -> None:
    now = datetime(2026, 8, 14, 12)
    legacy_peer = "https://t.me/route_alias"
    current_peer = "-1002300"
    session.add_all([
        Tenant(id=1, name="租户"),
        TgGroup(id=21, tenant_id=1, tg_peer_id=legacy_peer, title="路由群"),
        TgGroup(id=22, tenant_id=1, tg_peer_id=current_peer, title="路由群"),
        OperationTarget(id=31, tenant_id=1, target_type="group", tg_peer_id=legacy_peer, title="路由群"),
        OperationTarget(
            id=32,
            tenant_id=1,
            target_type="group",
            tg_peer_id=current_peer,
            title="路由群",
            username="route_alias",
        ),
        Task(
            id="route-repair-task",
            tenant_id=1,
            name="修复任务",
            type="group_ai_chat",
            status="running",
            config_revision=1,
            last_error="daily_group_target_ledger_missing",
            type_config={
                "target_group_id": 22,
                "target_operation_target_id": 32,
                "target_reference_revision": 1,
                "daily_message_target": 10,
            },
        ),
        TaskDayLedger(
            id="route-repair-ledger",
            tenant_id=1,
            task_id="route-repair-task",
            timezone_snapshot="Asia/Shanghai",
            timezone_revision=1,
            obligation_local_date=date(2026, 8, 14),
            period_start_at=now,
            deadline_at=now + timedelta(days=1),
            day_phase="full_day",
            planning_anchor_at=now,
        ),
        _target("ledger-target", 21, "route-repair-ledger", now),
        _target("orphan-target", 22, None, now),
        TaskGroupDailyMessageSlot(
            id="route-slot",
            tenant_id=1,
            task_id="route-repair-task",
            task_day_ledger_id="route-repair-ledger",
            target_operation_target_id=31,
            slot_kind="extra_volume",
            slot_ordinal=1,
        ),
    ])
    session.commit()


def _seed_safe_route_actions(session: Session) -> None:
    now = datetime(2026, 8, 14, 12, 1)
    peer_invalid = _route_action(
        "peer-invalid-route-action",
        status="closed_unknown",
        result={"error_code": FailureType.PEER_INVALID.value},
    )
    session.add_all([
        peer_invalid,
        _route_action("skipped-route-action", status="skipped"),
        _route_action("pending-route-action", status="pending"),
        ExecutionAttempt(
            id="peer-invalid-route-attempt",
            tenant_id=1,
            action_id=peer_invalid.id,
            attempt_no=1,
            status="failed",
            gateway_call_started_at=now,
            after_call_at=now,
            failure_type=FailureType.PEER_INVALID.value,
        ),
    ])
    session.commit()


def _route_action(action_id: str, *, status: str, result: dict | None = None) -> Action:
    return Action(
        id=action_id,
        tenant_id=1,
        task_id="route-repair-task",
        task_type="group_ai_chat",
        action_type="invite_group_account",
        scheduled_at=datetime(2026, 8, 14, 12, 1),
        created_at=datetime(2026, 8, 14, 12, 1),
        status=status,
        payload={"group_id": 22, "target_operation_target_id": 32},
        result=result or {},
    )


def _target(
    target_id: str,
    group_id: int,
    ledger_id: str | None,
    now: datetime,
) -> TaskGroupDailyTarget:
    return TaskGroupDailyTarget(
        id=target_id,
        tenant_id=1,
        task_id="route-repair-task",
        task_day_ledger_id=ledger_id,
        group_id=group_id,
        target_date=date(2026, 8, 14),
        configured_message_target=10,
        frozen_account_count=1,
        effective_message_target=10,
        planned_daily_target=10,
        daily_fulfillment_phase="full_day",
        scope_frozen_at=now,
        full_day_committed_at=now,
        created_at=now,
    )
