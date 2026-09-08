from __future__ import annotations

from dataclasses import replace

from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import AiContentScopeTakeoverBatch, AuditLog, DispatchClaimScope
from app.services._common import _now
from app.services.task_center.dispatch_runtime_control import record_dispatcher_shard_heartbeat
from app.services.task_center.release_cutover import VERIFIED, ReleaseIdentity
from scripts import manage_shared_dispatch_contract as manager
from scripts import release_worker_cutover as cutover
from scripts import takeover_ai_content_scope as scope_takeover
from scripts import takeover_all_task_fulfillment as full_takeover
from test_dispatch_runtime_activation import _settings

pytestmark = pytest.mark.no_postgres
IDENTITY = ReleaseIdentity("a" * 40, "b" * 64, "test-release", "test:release")


@pytest.fixture
def runtime(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version(version_num VARCHAR(64))"))
        for revision in ScriptDirectory(str(cutover.BACKEND_ROOT / "migrations")).get_heads():
            connection.execute(text("INSERT INTO alembic_version VALUES (:revision)"), {"revision": revision})
    for module in (manager, cutover, scope_takeover, full_takeover):
        monkeypatch.setattr(module, "SessionLocal", sessions)
    for module in (manager, cutover, scope_takeover):
        monkeypatch.setattr(module, "get_settings", lambda: _settings(account_shard_index=0))
    yield sessions
    engine.dispose()


def _ready(sessions):
    with sessions() as session:
        for index in range(2):
            record_dispatcher_shard_heartbeat(
                session, _settings(account_shard_index=index), worker_id=f"candidate-worker-{index}",
            )
        session.commit()


def test_real_upgrade_then_ordinary_cutover_reuses_batch(runtime, monkeypatch):
    first = cutover.prepare(IDENTITY, _now().isoformat())
    assert first["mode"] == "upgrade"
    _ready(runtime)
    result = cutover.complete(IDENTITY, first["plan_id"])
    assert result["verification"]["verification_state"] == "active_verified"
    first_batch_id = result["takeover_head_batch_id"]
    successor = replace(IDENTITY, sha="c" * 40, approval_ref="test:successor")
    plan = cutover.prepare(successor, _now().isoformat())
    assert plan["mode"] == "ordinary"
    def unexpected_takeover(*_args, **_kwargs):
        raise AssertionError("ordinary release must not scan historical tasks")
    monkeypatch.setattr(cutover, "run_takeover", unexpected_takeover)
    monkeypatch.setattr(cutover, "run_preview", unexpected_takeover)
    _ready(runtime)
    result = cutover.complete(successor, plan["plan_id"])
    assert result["takeover_head_batch_id"] == first_batch_id
    with runtime() as session:
        assert session.query(AiContentScopeTakeoverBatch).count() == 1
        assert session.query(AuditLog).filter(AuditLog.action == VERIFIED).count() == 2


def test_upgrade_failure_leaves_preparing_and_has_no_success_evidence(runtime, monkeypatch):
    plan = cutover.prepare(IDENTITY, _now().isoformat())
    _ready(runtime)
    def failed_takeover(**_kwargs):
        raise RuntimeError("specific_takeover_failure")
    monkeypatch.setattr(cutover, "run_takeover", failed_takeover)
    with pytest.raises(RuntimeError, match="specific_takeover_failure"):
        cutover.complete(IDENTITY, plan["plan_id"])
    with runtime() as session:
        assert session.scalar(select(DispatchClaimScope)).contract_activation_state == "preparing"
        assert session.scalar(select(AuditLog).where(AuditLog.action == VERIFIED)) is None


def test_schema_drift_stops_before_staging(runtime):
    with runtime.begin() as session:
        session.execute(text("DELETE FROM alembic_version"))
    with pytest.raises(ValueError, match="schema_revision_mismatch"):
        cutover.prepare(IDENTITY, _now().isoformat())
    with runtime() as session:
        assert session.scalar(select(DispatchClaimScope)) is None
