from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from threading import Barrier

import pytest
from sqlalchemy import delete, func, select

from app.database import Base, SessionLocal, engine
from app.models import (
    AccountPool,
    Action,
    AiContentScopeTakeoverBatch,
    AiContentScopeTakeoverItem,
    AiCoverageVariationIntent,
    DailyRuntimeStat,
    ExecutionAttempt,
    GatewayRequestEvidenceJournal,
    OperationTarget,
    ReviewQueue,
    RemoteReconcileCase,
    RuleSet,
    RuleSetVersion,
    RuntimeCleanupAudit,
    SearchRankDeboostClickReservation,
    Task,
    TaskAccountDailyCoverage,
    TaskHardHourlyBucket,
    TaskHardHourlyDeliveryCredit,
    TaskMembershipAdmissionItem,
    Tenant,
    TgAccount,
    TgGroup,
)
from app.services.task_center import runtime_retention
from app.services.task_center.runtime_retention import cleanup_runtime_details


TENANT_ID = 915_071
ACCOUNT_ID = 915_071
POOL_ID = 915_071
GROUP_ID = 915_071
TARGET_ID = 915_071
TASK_ID = "runtime-retention-pg-task"
OLD_AT = datetime(2000, 1, 1, 10, 0, 0)
TODAY = date(2000, 1, 10)
CUTOFF_DATE = date(2000, 1, 5)


def test_cleanup_clears_or_deletes_all_action_foreign_keys() -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    try:
        _seed_base()
        _seed_referenced_action()
        with SessionLocal() as session:
            assert cleanup_runtime_details(session, retention_days=5, today=TODAY, batch_size=10) == 3
            session.commit()
        _assert_references_removed()
    finally:
        _cleanup()


def test_cleanup_preserves_unknown_action_and_its_evidence() -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    try:
        _seed_base()
        _seed_referenced_action(status="unknown_after_send")
        with SessionLocal() as session:
            assert cleanup_runtime_details(session, retention_days=5, today=TODAY, batch_size=10) == 0
            session.commit()
            assert session.get(Action, "retention-action-referenced") is not None
            assert session.get(AiContentScopeTakeoverItem, "retention-takeover-item") is not None
            assert session.get(RemoteReconcileCase, "retention-remote-case") is not None
            assert session.get(GatewayRequestEvidenceJournal, "retention-journal") is not None
    finally:
        _cleanup()


def test_cleanup_reference_failure_rolls_back_summary_and_details(monkeypatch) -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    try:
        _seed_base()
        _seed_referenced_action()
        original_remove = runtime_retention._remove_action_references

        def fail_after_reference_deletes(session, action_ids):  # noqa: ANN001
            original_remove(session, action_ids)
            raise RuntimeError("injected_reference_delete_failure")

        monkeypatch.setattr(runtime_retention, "_remove_action_references", fail_after_reference_deletes)
        with SessionLocal() as session:
            with pytest.raises(RuntimeError, match="injected_reference_delete_failure"):
                cleanup_runtime_details(session, retention_days=5, today=TODAY, batch_size=10)
            session.rollback()
        with SessionLocal() as session:
            assert session.get(Action, "retention-action-referenced") is not None
            assert session.get(ExecutionAttempt, "retention-attempt") is not None
            assert session.get(AiContentScopeTakeoverItem, "retention-takeover-item") is not None
            assert session.get(RemoteReconcileCase, "retention-remote-case") is not None
            assert session.get(GatewayRequestEvidenceJournal, "retention-journal") is not None
            assert session.scalar(select(func.count()).select_from(DailyRuntimeStat)) == 0
            assert session.scalar(select(func.count()).select_from(RuntimeCleanupAudit)) == 0
    finally:
        _cleanup()


def test_two_cleanup_workers_claim_disjoint_batches_and_atomically_accumulate(monkeypatch) -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    try:
        _seed_base()
        _seed_actions(2)
        barrier = Barrier(2, timeout=5)
        original_batch = runtime_retention._runtime_detail_batch

        def synchronized_batch(session, cutoff_dt, batch_size):  # noqa: ANN001
            rows = original_batch(session, cutoff_dt, batch_size)
            barrier.wait()
            return rows

        monkeypatch.setattr(runtime_retention, "_runtime_detail_batch", synchronized_batch)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(_run_cleanup, range(2)))

        with SessionLocal() as session:
            total = session.scalar(select(DailyRuntimeStat.metric_value).where(
                DailyRuntimeStat.stat_date == OLD_AT.date(),
                DailyRuntimeStat.dimension_type == "global",
                DailyRuntimeStat.dimension_id == "all",
                DailyRuntimeStat.metric_name == "total",
            ))
            audits = session.scalar(select(func.count()).select_from(RuntimeCleanupAudit).where(
                RuntimeCleanupAudit.cleanup_date == CUTOFF_DATE,
            ))
            remaining = session.scalar(select(func.count()).select_from(Action).where(Action.tenant_id == TENANT_ID))
        assert results == [1, 1]
        assert total == 2
        assert audits == 2
        assert remaining == 0
    finally:
        _cleanup()


def _seed_base() -> None:
    with SessionLocal() as session:
        session.add(Tenant(id=TENANT_ID, name="runtime-retention-pg"))
        session.commit()
        session.add(AccountPool(id=POOL_ID, tenant_id=TENANT_ID, name="runtime-retention-pg"))
        session.add(TgGroup(id=GROUP_ID, tenant_id=TENANT_ID, tg_peer_id="retention-group", title="retention"))
        session.add(OperationTarget(
            id=TARGET_ID,
            tenant_id=TENANT_ID,
            tg_peer_id="retention-target",
            title="retention",
        ))
        session.add(Task(id=TASK_ID, tenant_id=TENANT_ID, name="retention", type="group_relay", status="running"))
        session.commit()
        session.add(TgAccount(
            id=ACCOUNT_ID,
            tenant_id=TENANT_ID,
            pool_id=POOL_ID,
            display_name="retention",
            phone_masked="retention-pg",
        ))
        session.commit()


def _seed_referenced_action(*, status: str = "success") -> None:
    with SessionLocal() as session:
        action = _action("referenced", status=status)
        session.add(action)
        session.flush()
        _add_account_references(session, action)
        _add_delivery_references(session, action)
        _add_recovery_evidence(session, action)
        session.commit()


def _add_account_references(session, action: Action) -> None:  # noqa: ANN001
    session.add(ExecutionAttempt(id="retention-attempt", tenant_id=TENANT_ID, action_id=action.id))
    session.add(ReviewQueue(id="retention-review", tenant_id=TENANT_ID, task_id=TASK_ID, action_id=action.id))
    session.add(
        TaskAccountDailyCoverage(
            id="retention-coverage",
            tenant_id=TENANT_ID,
            task_id=TASK_ID,
            group_id=GROUP_ID,
            account_id=ACCOUNT_ID,
            coverage_date=OLD_AT.date(),
            reserved_action_id=action.id,
            last_success_action_id=action.id,
        )
    )
    session.flush()
    session.add(
        AiCoverageVariationIntent(
            id="retention-variation",
            tenant_id=TENANT_ID,
            coverage_ledger_id="retention-coverage",
            action_id=action.id,
            content_variation_key="retention",
        )
    )


def _add_delivery_references(session, action: Action) -> None:  # noqa: ANN001
    session.add(
        TaskHardHourlyBucket(
            id=TARGET_ID,
            tenant_id=TENANT_ID,
            task_id=TASK_ID,
            operation_target_id=TARGET_ID,
            target_reference_revision=1,
            bucket_key="retention",
            bucket_start=OLD_AT,
            bucket_end=OLD_AT + timedelta(hours=1),
        )
    )
    session.flush()
    session.add(
        TaskHardHourlyDeliveryCredit(
            id=TARGET_ID,
            bucket_id=TARGET_ID,
            action_id=action.id,
            executed_at=OLD_AT,
            remote_message_id="retention-message",
        )
    )
    session.add(
        TaskMembershipAdmissionItem(
            id=TARGET_ID,
            tenant_id=TENANT_ID,
            task_id=TASK_ID,
            account_id=ACCOUNT_ID,
            target_id=TARGET_ID,
            membership_action_id=action.id,
            test_message_action_id=action.id,
            delete_action_id=action.id,
            rescue_action_id=action.id,
        )
    )
    session.add(
        SearchRankDeboostClickReservation(
            id="retention-reservation",
            tenant_id=TENANT_ID,
            task_id=TASK_ID,
            action_id=action.id,
            account_id=ACCOUNT_ID,
            account_pool_id=POOL_ID,
            keyword_hash="retention",
            local_date=OLD_AT.date(),
            hour_bucket=OLD_AT,
            expires_at=OLD_AT + timedelta(hours=1),
        )
    )


def _add_recovery_evidence(session, action: Action) -> None:  # noqa: ANN001
    session.add(
        AiContentScopeTakeoverBatch(
            id="retention-takeover-batch",
            dispatcher_scope="retention",
            cutoff_at=OLD_AT,
            actor="retention-test",
            classification_hash="batch-hash",
            classification_counts={"retention": 1},
            status="completed",
        )
    )
    session.add(
        AiContentScopeTakeoverItem(
            id="retention-takeover-item",
            batch_id="retention-takeover-batch",
            action_id=action.id,
            observed_action_state_hash="action-hash",
            classification="retention",
            classification_input_hash="input-hash",
            status="noop",
        )
    )
    session.add(
        RemoteReconcileCase(
            id="retention-remote-case",
            action_id=action.id,
            execution_attempt_id="retention-attempt",
            expected_action_state_hash="action-hash",
            expected_attempt_state_hash="attempt-hash",
        )
    )
    session.add(
        GatewayRequestEvidenceJournal(
            id="retention-journal",
            tenant_id=TENANT_ID,
            action_id=action.id,
            execution_attempt_id="retention-attempt",
            account_id=ACCOUNT_ID,
            gateway_request_identity="retention-request",
            request_fingerprint="request-hash",
            target_fingerprint="target-hash",
            result_fingerprint="result-hash",
            evidence_hash="evidence-hash",
        )
    )


def _seed_actions(count: int) -> None:
    with SessionLocal() as session:
        session.add_all(_action(str(index)) for index in range(count))
        session.commit()


def _action(suffix: str, *, status: str = "success") -> Action:
    return Action(
        id=f"retention-action-{suffix}",
        tenant_id=TENANT_ID,
        task_id=TASK_ID,
        task_type="group_relay",
        action_type="send_message",
        status=status,
        scheduled_at=OLD_AT,
        executed_at=OLD_AT,
        created_at=OLD_AT,
    )


def _run_cleanup(_worker: int) -> int:
    with SessionLocal() as session:
        deleted = cleanup_runtime_details(session, retention_days=5, today=TODAY, batch_size=1)
        session.commit()
        return deleted


def _assert_references_removed() -> None:
    with SessionLocal() as session:
        assert session.get(Action, "retention-action-referenced") is None
        coverage = session.get(TaskAccountDailyCoverage, "retention-coverage")
        admission = session.get(TaskMembershipAdmissionItem, TARGET_ID)
        assert coverage.reserved_action_id is None
        assert coverage.last_success_action_id is None
        assert admission.membership_action_id is None
        assert admission.test_message_action_id is None
        assert admission.delete_action_id is None
        assert admission.rescue_action_id is None
        assert session.get(AiCoverageVariationIntent, "retention-variation").action_id is None
        assert session.get(TaskHardHourlyDeliveryCredit, TARGET_ID) is None
        assert session.get(SearchRankDeboostClickReservation, "retention-reservation") is None
        assert session.get(AiContentScopeTakeoverItem, "retention-takeover-item") is None
        assert session.get(RemoteReconcileCase, "retention-remote-case") is None
        assert session.get(GatewayRequestEvidenceJournal, "retention-journal") is None
        assert session.get(AiContentScopeTakeoverBatch, "retention-takeover-batch") is not None
        audit = session.scalar(select(RuntimeCleanupAudit).where(
            RuntimeCleanupAudit.cleanup_date == CUTOFF_DATE,
        ))
        assert audit.deleted_counts["ai_content_scope_takeover_items"] == 1
        assert audit.deleted_counts["remote_reconcile_cases"] == 1
        assert audit.deleted_counts["gateway_request_evidence_journals"] == 1


def _cleanup() -> None:
    with SessionLocal() as session:
        action_ids = select(Action.id).where(Action.tenant_id == TENANT_ID)
        session.execute(delete(GatewayRequestEvidenceJournal).where(
            GatewayRequestEvidenceJournal.tenant_id == TENANT_ID,
        ))
        session.execute(delete(RemoteReconcileCase).where(RemoteReconcileCase.action_id.in_(action_ids)))
        session.execute(delete(AiContentScopeTakeoverItem).where(AiContentScopeTakeoverItem.action_id.in_(action_ids)))
        session.execute(delete(SearchRankDeboostClickReservation).where(
            SearchRankDeboostClickReservation.tenant_id == TENANT_ID,
        ))
        session.execute(delete(TaskHardHourlyDeliveryCredit).where(
            TaskHardHourlyDeliveryCredit.bucket_id == TARGET_ID,
        ))
        session.execute(delete(TaskHardHourlyBucket).where(TaskHardHourlyBucket.id == TARGET_ID))
        session.execute(delete(AiCoverageVariationIntent).where(
            AiCoverageVariationIntent.tenant_id == TENANT_ID,
        ))
        session.execute(delete(TaskAccountDailyCoverage).where(TaskAccountDailyCoverage.tenant_id == TENANT_ID))
        session.execute(delete(TaskMembershipAdmissionItem).where(TaskMembershipAdmissionItem.tenant_id == TENANT_ID))
        session.execute(delete(ExecutionAttempt).where(ExecutionAttempt.action_id.in_(action_ids)))
        session.execute(delete(ReviewQueue).where(ReviewQueue.tenant_id == TENANT_ID))
        session.execute(delete(Action).where(Action.tenant_id == TENANT_ID))
        session.execute(delete(AiContentScopeTakeoverBatch).where(
            AiContentScopeTakeoverBatch.id == "retention-takeover-batch",
        ))
        session.execute(delete(DailyRuntimeStat).where(DailyRuntimeStat.stat_date == OLD_AT.date()))
        session.execute(delete(RuntimeCleanupAudit).where(RuntimeCleanupAudit.cleanup_date == CUTOFF_DATE))
        session.execute(delete(Task).where(Task.tenant_id == TENANT_ID))
        session.execute(delete(OperationTarget).where(OperationTarget.tenant_id == TENANT_ID))
        session.execute(delete(TgGroup).where(TgGroup.tenant_id == TENANT_ID))
        session.execute(delete(TgAccount).where(TgAccount.tenant_id == TENANT_ID))
        session.execute(delete(AccountPool).where(AccountPool.tenant_id == TENANT_ID))
        session.execute(delete(RuleSetVersion).where(RuleSetVersion.tenant_id == TENANT_ID))
        session.execute(delete(RuleSet).where(RuleSet.tenant_id == TENANT_ID))
        session.execute(delete(Tenant).where(Tenant.id == TENANT_ID))
        session.commit()
