from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    ChannelMessage,
    ExecutionAttempt,
    FailureType,
    GatewayRequestEvidenceJournal,
    OperationTarget,
    Task,
    TaskDayLedger,
    Tenant,
    TgAccount,
    ViewFulfillmentObligation,
    ViewRemoteFact,
)
from scripts.recover_channel_view_false_target_terminal import (
    RecoveryRequest,
    apply_recovery,
    build_manifest,
    manifest_hash,
)


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        current.add(Tenant(id=1, name="恢复测试租户"))
        current.commit()
        yield current


def test_recovery_requires_safe_peer_failure_and_post_terminal_remote_fact(
    session: Session,
) -> None:
    task = _seed_false_terminal(session, mutation_state="false")
    request = _request(task.id)

    manifest = build_manifest(session, request, lock=False)

    assert manifest["candidate"] is True
    assert manifest["post_terminal_remote_facts"]["count"] == 1
    approved = RecoveryRequest(
        **{**request.__dict__, "apply": True, "expected_state_hash": manifest_hash(manifest)}
    )
    new_epoch = apply_recovery(session, approved, manifest)
    session.commit()

    assert task.status == "running"
    assert task.task_lifecycle_epoch == new_epoch == 3
    assert task.next_run_at is not None
    assert task.last_error == ""
    assert "target_terminal" not in task.stats


def test_recovery_blocks_unknown_gateway_mutation(
    session: Session,
) -> None:
    task = _seed_false_terminal(session, mutation_state="unknown")

    manifest = build_manifest(session, _request(task.id), lock=False)

    assert manifest["candidate"] is False
    assert "peer_invalid_failure_not_proven_safe" in manifest["blocking_reasons"]


def _request(task_id: str) -> RecoveryRequest:
    return RecoveryRequest(
        task_id=task_id,
        deployed_sha="a" * 40,
        apply=False,
        expected_state_hash="",
        actor="codex-production-repair",
        approval_ref="incident-20260811",
    )


def _seed_false_terminal(session: Session, *, mutation_state: str) -> Task:
    terminal_at = datetime(2026, 8, 11, 9, 44, 46)
    task = Task(
        id=f"false-terminal-{mutation_state}",
        tenant_id=1,
        name="误终态浏览任务",
        type="channel_view",
        status="failed",
        fulfillment_contract_version="fact_first_v3",
        task_lifecycle_epoch=2,
        last_error="Could not find the input entity",
        stats={"target_terminal": True, "target_terminal_at": terminal_at.isoformat()},
    )
    channel = OperationTarget(
        tenant_id=1,
        target_type="channel",
        tg_peer_id="-1009001",
        title="仍可访问频道",
    )
    failed_account = TgAccount(
        tenant_id=1,
        display_name="解析失败账号",
        phone_masked="failed",
    )
    success_account = TgAccount(
        tenant_id=1,
        display_name="浏览成功账号",
        phone_masked="success",
    )
    session.add_all([task, channel, failed_account, success_account])
    session.flush()
    message = ChannelMessage(
        tenant_id=1,
        channel_target_id=channel.id,
        message_id=5791,
    )
    ledger = TaskDayLedger(
        tenant_id=1,
        task_id=task.id,
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=date(2026, 8, 11),
        period_start_at=terminal_at - timedelta(hours=9),
        deadline_at=terminal_at + timedelta(hours=15),
        day_phase="full_day_committed",
        planning_anchor_at=terminal_at - timedelta(hours=9),
    )
    session.add_all([message, ledger])
    session.flush()
    failed_action = Action(
        id=f"failed-action-{mutation_state}",
        tenant_id=1,
        task_id=task.id,
        task_type="channel_view",
        action_type="view_message",
        account_id=failed_account.id,
        status="failed",
    )
    session.add(failed_action)
    session.flush()
    attempt = ExecutionAttempt(
        id=f"failed-attempt-{mutation_state}",
        tenant_id=1,
        action_id=failed_action.id,
        account_id=failed_account.id,
        status="failed",
        failure_type=FailureType.PEER_INVALID.value,
        gateway_call_started_at=terminal_at,
        after_call_at=terminal_at,
    )
    session.add(attempt)
    session.flush()
    session.add(GatewayRequestEvidenceJournal(
        tenant_id=1,
        action_id=failed_action.id,
        execution_attempt_id=attempt.id,
        account_id=failed_account.id,
        gateway_request_identity=f"request-{mutation_state}",
        request_fingerprint="1" * 64,
        target_fingerprint="2" * 64,
        result_fingerprint="3" * 64,
        evidence_hash="4" * 64,
        failure_code=FailureType.PEER_INVALID.value,
        remote_mutation_state=mutation_state,
        state="recorded",
    ))
    obligation = ViewFulfillmentObligation(
        tenant_id=1,
        task_day_ledger_id=ledger.id,
        channel_message_id=message.id,
        account_id=success_account.id,
        status="confirmed",
    )
    session.add(obligation)
    session.flush()
    session.add(ViewRemoteFact(
        tenant_id=1,
        obligation_id=obligation.id,
        obligation_local_date=ledger.obligation_local_date,
        target_peer_id=channel.tg_peer_id,
        channel_message_id=message.id,
        account_id=success_account.id,
        remote_confirmed_at=terminal_at + timedelta(seconds=1),
        created_at=terminal_at + timedelta(seconds=1),
    ))
    session.commit()
    return task
