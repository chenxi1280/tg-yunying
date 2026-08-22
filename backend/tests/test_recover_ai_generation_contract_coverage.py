from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    AuditLog,
    ExecutionAttempt,
    Task,
    TaskAccountDailyCoverage,
    Tenant,
    TgAccount,
    TgGroup,
)
from app.services.task_center.fulfillment_activation import CURRENT_CONTRACT_VERSION
from scripts.recover_ai_generation_contract_coverage import (
    RecoveryRequest,
    apply_recovery,
    recovery_snapshot,
    snapshot_hash,
)


pytestmark = pytest.mark.no_postgres
LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        yield current


def _request(*, expected_state_hash: str = "") -> RecoveryRequest:
    return RecoveryRequest(
        task_ids=("generation-contract-task",),
        blocker_code="ai_generation_slot_mapping_mismatch",
        apply=bool(expected_state_hash),
        expected_state_hash=expected_state_hash,
        actor="test-operator",
        approval_ref="incident-20260822-ai-generation-contract",
    )


def _seed(session: Session) -> TaskAccountDailyCoverage:
    session.add(Tenant(id=1, name="测试租户"))
    session.add(TgGroup(
        id=21,
        tenant_id=1,
        tg_peer_id="-10021",
        title="目标群",
        active_window="00:00-23:59",
    ))
    session.add(TgAccount(
        id=31,
        tenant_id=1,
        display_name="测试账号",
        phone_masked="***0031",
        status="在线",
    ))
    task = Task(
        id="generation-contract-task",
        tenant_id=1,
        name="生成合同恢复任务",
        type="group_ai_chat",
        status="running",
        fulfillment_contract_version=CURRENT_CONTRACT_VERSION,
    )
    session.add(task)
    session.flush()
    action = Action(
        id="generation-contract-action",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="send_message",
        account_id=31,
        status="failed",
        scheduled_at=datetime.now(LOCAL_TIMEZONE),
        result={"error_code": "ai_generation_slot_mapping_mismatch"},
    )
    coverage = TaskAccountDailyCoverage(
        id="generation-contract-coverage",
        tenant_id=1,
        task_id=task.id,
        group_id=21,
        account_id=31,
        coverage_date=datetime.now(LOCAL_TIMEZONE).date(),
        state="blocked",
        blocker_code="ai_generation_slot_mapping_mismatch",
        blocker_stage="generation_contract",
        blocker_detail="historical slot identity mismatch",
        recovery_path="generation_contract_repair",
        last_action_id=action.id,
    )
    session.add_all([action, coverage])
    session.commit()
    return coverage


def test_snapshot_hash_is_order_independent() -> None:
    assert snapshot_hash({"b": 2, "a": 1}) == snapshot_hash({"a": 1, "b": 2})


def test_recovery_requires_matching_preview_and_reads_back_clear_blocker(
    session: Session,
) -> None:
    coverage = _seed(session)
    preview = recovery_snapshot(session, _request())

    assert preview["matched_count"] == 1
    assert preview["conflicts"] == []
    with pytest.raises(RuntimeError, match="state hash changed"):
        apply_recovery(session, _request(expected_state_hash="0" * 64))
    session.rollback()

    locked_preview, recovered_ids = apply_recovery(
        session,
        _request(expected_state_hash=snapshot_hash(preview)),
    )
    session.refresh(coverage)
    audit = session.scalar(select(AuditLog))

    assert locked_preview == preview
    assert recovered_ids == [coverage.id]
    assert coverage.state == "ready"
    assert coverage.blocker_code == ""
    assert coverage.blocker_stage == ""
    assert coverage.reserved_action_id is None
    assert audit is not None
    assert audit.target_id == "generation-contract-task"
    assert "incident-20260822-ai-generation-contract" in audit.detail


def test_recovery_preview_rejects_old_action_with_gateway_marker(session: Session) -> None:
    coverage = _seed(session)
    timestamp = datetime.now(LOCAL_TIMEZONE)
    session.add(ExecutionAttempt(
        tenant_id=1,
        action_id=coverage.last_action_id,
        account_id=coverage.account_id,
        attempt_no=1,
        status="failed",
        before_call_at=timestamp,
        gateway_call_started_at=timestamp,
        after_call_at=timestamp,
    ))
    session.commit()

    preview = recovery_snapshot(session, _request())

    assert preview["matched_count"] == 1
    assert preview["conflicts"] == [{
        "coverage_id": coverage.id,
        "reason": "gateway_already_started",
    }]
