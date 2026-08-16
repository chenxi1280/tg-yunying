from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import ValidationError
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountPacingReservation,
    Action,
    FulfillmentRemoteFact,
    Task,
    TaskGroupDailyMessageSlot,
    Tenant,
)
from app.schemas.task_center import ChannelCommentConfig, GroupAIChatConfig
from app.services.task_center import account_pacing_guard
from app.services.task_center.account_pacing_guard import (
    _earliest_available_time,
    reserve_account_pacing,
)
from app.services.task_center.ai_generation_parallel import _generation_job
from app.services.task_center.ai_pacing import _align_quantity_slots, _available_quantity_slots
from app.services.task_center.pacing_persistence import (
    PacingOwnerImmutableConflict,
    freeze_action_pacing,
    freeze_pacing_owner,
)
from pacing_contract_test_support import pacing_engine


pytestmark = pytest.mark.no_postgres


def _load_migration_0150() -> ModuleType:
    migration_path = Path(__file__).parents[1] / "migrations/versions/0150_pacing_slot_fields.py"
    spec = importlib.util.spec_from_file_location("migration_0150", migration_path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def _create_0150_prerequisites(connection: Connection) -> None:
    statements = (
        "CREATE TABLE actions (id VARCHAR(36) PRIMARY KEY, tenant_id INTEGER, "
        "account_id INTEGER, status VARCHAR(40), scheduled_at DATETIME)",
        "CREATE TABLE execution_attempts (id VARCHAR(36) PRIMARY KEY, tenant_id INTEGER, "
        "account_id INTEGER, status VARCHAR(40), after_call_at DATETIME)",
        "CREATE TABLE fulfillment_remote_facts (fact_id VARCHAR(36) PRIMARY KEY, "
        "tenant_id INTEGER, action_id VARCHAR(36), fact_kind VARCHAR(40), observed_at DATETIME)",
    )
    for statement in statements:
        connection.execute(text(statement))
    obligation_tables = (
        "comment_fulfillment_obligations",
        "reaction_fulfillment_obligations",
        "view_fulfillment_obligations",
        "task_group_daily_message_slots",
        "generation_jobs",
    )
    for table in obligation_tables:
        connection.execute(text(f"CREATE TABLE {table} (id VARCHAR(36) PRIMARY KEY)"))
    connection.execute(text("CREATE TABLE tenants (id INTEGER PRIMARY KEY)"))
    connection.execute(text("CREATE TABLE tasks (id VARCHAR(36) PRIMARY KEY)"))
    connection.execute(text("CREATE TABLE tg_accounts (id INTEGER PRIMARY KEY)"))


def test_account_pacing_uses_one_tenant_scoped_timeline_query(monkeypatch) -> None:
    monkeypatch.setattr(
        account_pacing_guard,
        "get_settings",
        lambda: SimpleNamespace(account_soft_pacing_min_gap_seconds=20),
    )
    engine = pacing_engine()
    statements: list[str] = []
    event.listen(
        engine,
        "before_cursor_execute",
        lambda _conn, _cursor, statement, _params, _context, _many: statements.append(statement),
    )
    due = datetime(2026, 8, 16, 10, 0)
    with Session(engine) as session:
        reservation = reserve_account_pacing(
            session,
            tenant_id=1,
            task_id="pacing-task",
            account_id=9101,
            slot_key="bounded:1",
            due_at=due,
            deadline_at=due + timedelta(hours=2),
        )

    selects = [statement.lower() for statement in statements if statement.lstrip().upper().startswith("SELECT")]
    assert reservation.effective_claim_at == due
    assert len(selects) == 3
    timeline = [statement for statement in selects if "union all" in statement]
    assert len(timeline) == 1
    assert timeline[0].count("tenant_id") >= 3


def test_account_pacing_pages_without_truncating_dense_conflicts(monkeypatch) -> None:
    monkeypatch.setattr(
        account_pacing_guard,
        "get_settings",
        lambda: SimpleNamespace(account_soft_pacing_min_gap_seconds=20),
    )
    engine = pacing_engine()
    due = datetime(2026, 8, 16, 10, 0)
    with Session(engine) as session:
        session.add_all([
            AccountPacingReservation(
                tenant_id=1,
                task_id="pacing-task",
                account_id=9101,
                pacing_slot_key=f"existing:{index}",
                policy_version="account_soft_pacing_v1",
                due_at=due,
                release_not_before_at=due,
                effective_claim_at=due + timedelta(seconds=20 * index),
            )
            for index in range(130)
        ])
        session.commit()
        reservation = reserve_account_pacing(
            session,
            tenant_id=1,
            task_id="pacing-task",
            account_id=9101,
            slot_key="after-dense-window",
            due_at=due,
            deadline_at=due + timedelta(hours=2),
        )

    assert reservation.effective_claim_at == due + timedelta(seconds=20 * 130)


def test_frozen_pacing_owner_accepts_same_beijing_wall_time_from_postgres() -> None:
    due = datetime(2026, 8, 16, 10, 0)
    release = due + timedelta(minutes=5)
    timezone = ZoneInfo("Asia/Shanghai")
    owner = SimpleNamespace(
        pacing_contract_version="deterministic_stratified_v1",
        pacing_plan_hash="plan-hash",
        pacing_slot_ordinal=0,
        pacing_plan_total=1,
        pacing_due_at=due.replace(tzinfo=timezone),
        release_not_before_at=release.replace(tzinfo=timezone),
    )

    frozen_release = freeze_pacing_owner(
        owner,
        plan_hash="plan-hash",
        slot_ordinal=0,
        plan_total=1,
        due_at=due,
        release_not_before_at=release,
    )

    assert frozen_release == release.replace(tzinfo=timezone)


def _frozen_owner_with_total(plan_total: int) -> SimpleNamespace:
    due = datetime(2026, 8, 16, 10, 0)
    timezone = ZoneInfo("Asia/Shanghai")
    return SimpleNamespace(
        pacing_contract_version="deterministic_stratified_v1",
        pacing_plan_hash="plan-hash",
        pacing_slot_ordinal=41,
        pacing_plan_total=plan_total,
        pacing_due_at=due.replace(tzinfo=timezone),
        release_not_before_at=due.replace(tzinfo=timezone),
    )


def test_freeze_pacing_owner_allows_monotonic_target_increase() -> None:
    """目标上调迁移：identity 一致且 plan_total 单调上调时允许升级冻结的 total/due/release。"""
    owner = _frozen_owner_with_total(877)
    new_due = datetime(2026, 8, 16, 12, 0)
    new_release = new_due + timedelta(minutes=5)

    frozen_release = freeze_pacing_owner(
        owner,
        plan_hash="plan-hash",
        slot_ordinal=41,
        plan_total=1064,
        due_at=new_due,
        release_not_before_at=new_release,
    )

    assert owner.pacing_plan_total == 1064
    assert owner.pacing_due_at == new_due
    assert frozen_release == new_release
    assert owner.release_not_before_at == new_release


def test_freeze_pacing_owner_rejects_identity_regression() -> None:
    """plan_hash/slot_ordinal 漂移、plan_total 下调、total 不变的 due 漂移仍必须拒绝。"""
    with pytest.raises(PacingOwnerImmutableConflict):
        freeze_pacing_owner(
            _frozen_owner_with_total(877),
            plan_hash="other-hash",
            slot_ordinal=41,
            plan_total=1064,
            due_at=datetime(2026, 8, 16, 12, 0),
        )
    with pytest.raises(PacingOwnerImmutableConflict):
        freeze_pacing_owner(
            _frozen_owner_with_total(1064),
            plan_hash="plan-hash",
            slot_ordinal=41,
            plan_total=877,
            due_at=datetime(2026, 8, 16, 12, 0),
        )
    drift_owner = _frozen_owner_with_total(877)
    drift_owner.pacing_due_at = drift_owner.pacing_due_at + timedelta(hours=3)
    with pytest.raises(PacingOwnerImmutableConflict):
        freeze_pacing_owner(
            drift_owner,
            plan_hash="plan-hash",
            slot_ordinal=41,
            plan_total=877,
            due_at=datetime(2026, 8, 16, 10, 0),
        )


def test_freeze_pacing_owner_tolerates_owner_without_plan_total_field() -> None:
    """旧语义兼容：owner 无 pacing_plan_total 属性时不得因 total 校验误报冲突。"""
    due = datetime(2026, 8, 16, 10, 0)
    owner = SimpleNamespace(
        pacing_contract_version="deterministic_stratified_v1",
        pacing_plan_hash="plan-hash",
        pacing_slot_ordinal=3,
        pacing_due_at=due.replace(tzinfo=ZoneInfo("Asia/Shanghai")),
        release_not_before_at=due.replace(tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    frozen_release = freeze_pacing_owner(
        owner,
        plan_hash="plan-hash",
        slot_ordinal=3,
        plan_total=500,
        due_at=due,
    )

    assert frozen_release is not None


def test_ai_slot_alignment_is_linear_in_available_slots() -> None:
    reads = [0]

    class CountingSlot:
        def __init__(self, coverage_id: str) -> None:
            self.coverage_id = coverage_id

        @property
        def task_account_daily_coverage_id(self) -> str:
            reads[0] += 1
            return self.coverage_id

    total = 100
    available = [CountingSlot(f"coverage-{index}") for index in reversed(range(total))]
    coverage = {
        index: SimpleNamespace(id=f"coverage-{index}", target_count=1, confirmed_count=0)
        for index in range(total)
    }

    selected = _align_quantity_slots(available, coverage, list(range(total)))

    assert [slot.coverage_id for slot in selected] == [f"coverage-{index}" for index in range(total)]
    assert reads[0] <= total * 3


def test_ai_slot_query_fetches_only_current_batch_owners() -> None:
    engine = pacing_engine()
    with Session(engine) as session:
        session.add_all([
            TaskGroupDailyMessageSlot(
                id=slot_id,
                tenant_id=1,
                task_id="pacing-task",
                task_day_ledger_id="ledger-1",
                target_operation_target_id=1,
                task_account_daily_coverage_id=coverage_id,
                slot_kind="quantity",
                slot_ordinal=ordinal,
            )
            for slot_id, coverage_id, ordinal in (
                ("irrelevant", "coverage-other", 1),
                ("specific", "coverage-current", 2),
                ("unassigned-1", None, 3),
                ("unassigned-2", None, 4),
            )
        ])
        session.commit()
        task = session.get(Task, "pacing-task")
        rows = _available_quantity_slots(
            session,
            task,
            "ledger-1",
            expected_coverage_ids=["coverage-current", ""],
        )

    assert [row.id for row in rows] == ["specific", "unassigned-1"]


def test_group_message_pacing_ordinal_survives_reload_for_action_binding() -> None:
    engine = pacing_engine()
    with Session(engine) as session:
        owner = TaskGroupDailyMessageSlot(
            id="durable-pacing-owner",
            tenant_id=1,
            task_id="pacing-task",
            task_day_ledger_id="ledger-1",
            target_operation_target_id=1,
            slot_kind="quantity",
            slot_ordinal=4,
            pacing_slot_ordinal=4,
        )
        session.add(owner)
        session.commit()
        session.expire_all()

        reloaded = session.get(TaskGroupDailyMessageSlot, owner.id)
        action = Action(
            id="durable-pacing-action",
            tenant_id=1,
            task_id="pacing-task",
            task_type="group_ai_chat",
            action_type="send_message",
        )
        freeze_action_pacing(action, reloaded, slot_key="ai:durable-pacing-owner")

    assert action.pacing_slot_ordinal == 4


def test_account_pacing_finds_gap_before_unrelated_future_reservation() -> None:
    desired = datetime(2026, 8, 16, 9, 0)
    future = datetime(2026, 8, 16, 10, 0)

    assert _earliest_available_time(
        desired,
        [future],
        timedelta(seconds=20),
    ) == desired


def test_two_stage_flag_is_supported_by_strict_task_schemas() -> None:
    group = GroupAIChatConfig(
        target_input="@group",
        ai_two_stage_enabled=True,
        ai_model="generator-v1",
        ai_semantic_reviewer_model="reviewer-v1",
    )
    comment = ChannelCommentConfig(
        target_input="@channel",
        ai_two_stage_enabled=True,
        ai_model="generator-v1",
        ai_semantic_reviewer_model="reviewer-v1",
    )

    assert group.ai_two_stage_enabled is True
    assert comment.ai_two_stage_enabled is True
    assert group.ai_semantic_reviewer_model == "reviewer-v1"
    assert comment.ai_semantic_reviewer_model == "reviewer-v1"


@pytest.mark.parametrize("config_type,target", [
    (GroupAIChatConfig, {"target_input": "@group"}),
    (ChannelCommentConfig, {"target_input": "@channel"}),
])
def test_two_stage_schema_requires_independent_reviewer(config_type, target) -> None:
    with pytest.raises(ValidationError, match="必须显式配置生成模型"):
        config_type(
            **target,
            ai_two_stage_enabled=True,
            ai_semantic_reviewer_model="reviewer-model",
        )

    with pytest.raises(ValidationError, match="必须配置独立语义评审模型"):
        config_type(**target, ai_two_stage_enabled=True, ai_model="generator-model")

    for generator, reviewer in (
        ("same-model", "same-model"),
        ("mimo v2.5", "xiaomi mimo-v2.5"),
        ("custom  reviewer", "CUSTOM REVIEWER"),
    ):
        with pytest.raises(ValidationError, match="必须与生成模型不同"):
            config_type(
                **target,
                ai_two_stage_enabled=True,
                ai_model=generator,
                ai_semantic_reviewer_model=reviewer,
            )


def test_generation_job_freezes_timing_context_and_revision_evidence() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    scheduled_at = datetime(2026, 8, 16, 10, 30)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="generation evidence tenant"))
        session.add(Task(
            id="generation-evidence-task",
            tenant_id=1,
            name="generation evidence",
            type="group_ai_chat",
        ))
        action = Action(
            id="generation-evidence-action",
            tenant_id=1,
            task_id="generation-evidence-task",
            task_type="group_ai_chat",
            action_type="send_message",
            obligation_type="quantity_slot",
            obligation_id="quantity-slot-1",
            scheduled_at=scheduled_at,
            effective_claim_at=scheduled_at,
            assignment_revision=3,
            intent_revision=4,
            payload={
                "context_message_ids": [11, 12],
                "reply_to_message_id": "9001",
                "context_snapshot_version": 2,
            },
        )
        session.add(action)
        session.flush()

        job = _generation_job(session, action)

        assert job.generation_not_before_at == scheduled_at
        assert len(job.context_snapshot_hash) == 64
        assert job.assignment_revision == 3
        assert job.intent_revision == 4


def test_remote_fact_model_has_action_lookup_index() -> None:
    indexes = {index.name: tuple(column.name for column in index.columns) for index in FulfillmentRemoteFact.__table__.indexes}

    assert indexes["ix_fulfillment_remote_fact_action_typed"] == (
        "tenant_id",
        "action_id",
        "fact_kind",
        "observed_at",
    )


def test_0150_upgrade_and_downgrade_execute_with_real_alembic_operations() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    migration = _load_migration_0150()

    with engine.begin() as connection:
        _create_0150_prerequisites(connection)
        context = MigrationContext.configure(connection)
        migration.op = Operations(context)
        assert "CREATE INDEX CONCURRENTLY" in migration._timeline_create_sql(
            migration.TIMELINE_INDEXES[0],
            postgres=True,
        )
        migration.upgrade()

        assert "effective_claim_at" in {
            column["name"] for column in inspect(connection).get_columns("actions")
        }
        assert inspect(connection).has_table("account_pacing_reservations")
        assert "ix_actions_account_pacing_timeline" in {
            index["name"] for index in inspect(connection).get_indexes("actions")
        }
        assert "ix_execution_attempts_account_pacing_timeline" in {
            index["name"] for index in inspect(connection).get_indexes("execution_attempts")
        }
        assert "ix_fulfillment_remote_fact_action_typed" in {
            index["name"] for index in inspect(connection).get_indexes("fulfillment_remote_facts")
        }

        migration.downgrade()
        assert not inspect(connection).has_table("account_pacing_reservations")
