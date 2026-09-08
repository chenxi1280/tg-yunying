from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AiContentScopeTakeoverBatch, AuditLog
from app.services._common import _now
from app.services.task_center.dispatch_runtime_control import (
    activate_dispatch_runtime_contract, record_dispatcher_shard_heartbeat,
)
from app.services.task_center.release_cutover import (
    ACTIVATED, VERIFIED, ReleaseIdentity, load_cutover_plan, prepare_cutover,
    record_verified_cutover, verify_reused_batch,
)
from app.services.task_center.release_cutover_fingerprint import contract_source_fingerprint
from test_dispatch_runtime_activation import _settings

pytestmark = pytest.mark.no_postgres
FIRST = ReleaseIdentity("a" * 40, "b" * 64, "release-owner", "release:first")
NEXT = replace(FIRST, sha="c" * 40, approval_ref="release:next")


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _baseline(session):
    plan = prepare_cutover(session, _settings(account_shard_index=0), FIRST)
    batch = AiContentScopeTakeoverBatch(
        id="completed-batch", dispatcher_scope="task_center_dispatch",
        cutoff_at=_now(), actor=FIRST.actor, classification_hash="f" * 64,
        status="completed", config_version=plan["contract_version"],
    )
    session.add(batch)
    for index in range(2):
        record_dispatcher_shard_heartbeat(
            session, _settings(account_shard_index=index), worker_id=f"worker-{index}",
        )
    activate_dispatch_runtime_contract(
        session, _settings(account_shard_index=0), takeover_head_batch_id=batch.id,
    )
    session.add(AuditLog(
        actor=FIRST.actor, action=ACTIVATED, target_type="dispatch_claim_scope",
        target_id=plan["scope_id"], detail=json.dumps({
            "takeover_head_batch_id": batch.id, "approval_ref": FIRST.approval_ref,
        }),
    ))
    session.flush()
    record_verified_cutover(session, plan, batch.id)
    session.commit()
    return plan, batch


def test_first_release_requires_full_upgrade(session):
    plan = prepare_cutover(session, _settings(account_shard_index=0), FIRST)
    assert plan["mode"] == "upgrade"
    assert plan["reason"] == "initial_scope"
    assert plan["takeover_head_batch_id"] == ""
    assert session.scalar(select(AuditLog).where(AuditLog.action == VERIFIED)) is None


def test_ordinary_release_reuses_real_completed_batch_and_stages_writers(session):
    _baseline(session)
    plan = prepare_cutover(session, _settings(account_shard_index=0), NEXT)
    assert plan["mode"] == "ordinary"
    assert plan["takeover_head_batch_id"] == "completed-batch"
    assert plan["source_evidence_id"] is not None
    assert session.query(AiContentScopeTakeoverBatch).count() == 1
    assert load_cutover_plan(session, plan["plan_id"], NEXT) == plan
    verify_reused_batch(session, _settings(account_shard_index=0), plan)


def test_changed_source_contract_requires_upgrade(session):
    _baseline(session)
    plan = prepare_cutover(session, _settings(account_shard_index=0), replace(NEXT, source_fingerprint="d" * 64))
    assert (plan["mode"], plan["reason"]) == ("upgrade", "contract_source_changed")
    assert not plan["takeover_head_batch_id"]


def test_unrecorded_activation_requires_upgrade(session):
    old, _ = _baseline(session)
    session.add(AuditLog(
        actor="legacy-release", action=ACTIVATED, target_type="dispatch_claim_scope",
        target_id=old["scope_id"], detail="{}",
    ))
    session.flush()
    plan = prepare_cutover(session, _settings(account_shard_index=0), NEXT)
    assert plan["reason"] == "activation_changed"


def test_incomplete_takeover_evidence_is_error_not_an_automatic_upgrade(session):
    _, batch = _baseline(session)
    batch.status = "applying"
    session.flush()
    with pytest.raises(ValueError, match="takeover_chain_incomplete"):
        prepare_cutover(session, _settings(account_shard_index=0), NEXT)


def test_reused_batch_is_checked_again_after_prepare(session):
    _, batch = _baseline(session)
    plan = prepare_cutover(session, _settings(account_shard_index=0), NEXT)
    batch.config_version = "other-contract"
    session.flush()
    with pytest.raises(ValueError, match="takeover_contract_mismatch"):
        verify_reused_batch(session, _settings(account_shard_index=0), plan)


def test_completed_or_superseded_plan_cannot_be_replayed(session):
    old, _ = _baseline(session)
    with pytest.raises(ValueError, match="already_activated"):
        load_cutover_plan(session, old["plan_id"], FIRST)
    plan = prepare_cutover(session, _settings(account_shard_index=0), NEXT)
    prepare_cutover(session, _settings(account_shard_index=0), replace(NEXT, sha="e" * 40))
    with pytest.raises(ValueError, match="superseded"):
        load_cutover_plan(session, plan["plan_id"], NEXT)


def test_identity_drift_and_missing_activation_prevent_success_evidence(session):
    plan = prepare_cutover(session, _settings(account_shard_index=0), FIRST)
    with pytest.raises(ValueError, match="identity_changed"):
        load_cutover_plan(session, plan["plan_id"], NEXT)
    with pytest.raises(ValueError, match="activation_evidence_missing"):
        record_verified_cutover(session, plan, "batch")
    assert session.scalar(select(AuditLog).where(AuditLog.action == VERIFIED)) is None


def test_repository_contract_fingerprint_is_complete_and_deterministic():
    root = Path(__file__).resolve().parents[1]
    first = contract_source_fingerprint(root)
    assert len(first) == 64
    assert contract_source_fingerprint(root) == first
