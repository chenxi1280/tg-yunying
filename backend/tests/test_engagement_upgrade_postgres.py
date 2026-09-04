"""Upgrade a populated pre-engine schema without touching the public QA schema."""
from types import SimpleNamespace

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from app import config as app_config
from app.database import BACKEND_DIR, Base, engine
from migrations.legacy_bootstrap import legacy_bootstrap_metadata


pytestmark = pytest.mark.allow_missing_rule_binding
QA_SCHEMA = "engine_incremental_upgrade_qa"


@pytest.fixture
def upgrade_database(monkeypatch):
    with engine.begin() as connection:
        assert connection.scalar(text("SELECT current_database()")) == "tg_yunying_test"
        connection.execute(text("CREATE SCHEMA engine_incremental_upgrade_qa"))
    url = engine.url.update_query_dict({"options": f"-csearch_path={QA_SCHEMA} -ctimezone=Asia/Shanghai"})
    isolated = create_engine(url)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(app_config, "get_settings", lambda: SimpleNamespace(
                database_url=url.render_as_string(hide_password=False)))
            yield isolated
    finally:
        isolated.dispose()
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA engine_incremental_upgrade_qa CASCADE"))


def _upgrade(revision):
    configuration = Config(str(BACKEND_DIR / "alembic.ini"))
    configuration.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    command.upgrade(configuration, revision)


def _seed_legacy(database):
    tables = legacy_bootstrap_metadata(Base.metadata).tables
    with database.begin() as connection:
        assert connection.scalar(text("SELECT current_schema()")) == QA_SCHEMA
        assert "provider_http_exchanges" not in inspect(connection).get_table_names()
        connection.execute(tables["tenants"].insert().values(id=901, name="QA legacy tenant"))
        connection.execute(tables["account_pools"].insert().values(
            id=901, tenant_id=901, name="QA pool", pool_purpose="normal"))
        connection.execute(tables["tasks"].insert().values(
            id="QA-legacy-task", tenant_id=901, name="QA task", type="group_ai_chat", status="paused"))
        connection.execute(tables["operation_targets"].insert().values(
            id=901, tenant_id=901, target_type="channel", tg_peer_id="-100901", title="QA channel"))
        connection.execute(tables["channel_messages"].insert().values(
            id=901, tenant_id=901, channel_target_id=901, message_id=1, content_preview="retained source"))


def test_populated_0196_upgrade_preserves_records_and_runs_backfills(upgrade_database):
    _upgrade("0196_comment_plan_safety")
    _seed_legacy(upgrade_database)
    _upgrade("head")
    with upgrade_database.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0223_burst_negative_outcome"
        assert connection.scalar(text("SELECT name FROM tenants WHERE id=901")) == "QA legacy tenant"
        assert connection.scalar(text("SELECT status FROM tasks WHERE id='QA-legacy-task'")) == "paused"
        assert connection.execute(text(
            "SELECT tenant_id, account_pool_id, rolling_window_days FROM account_fleet_activity_policy_revisions"
        )).one() == (901, 901, 3)
        assert connection.scalar(text("SELECT count(*) FROM provider_http_exchanges")) == 0
        assert connection.scalar(text("SELECT count(*) FROM execution_timing_profile_revisions")) == 0
        inspector = inspect(connection)
        assert connection.execute(text(
            "SELECT content_preview, grouped_id, source_metadata FROM channel_messages WHERE id=901"
        )).one() == ("retained source", "", {})
        _assert_channel_engine_schema(inspector)
        assert "action_class" in {c["name"] for c in inspector.get_columns("account_pacing_reservations")}
        names = {c["name"] for c in inspector.get_unique_constraints("channel_message_comments")}
        assert "uq_channel_message_comment_peer_identity" in names
        assert "uq_channel_message_comment_legacy_identity" not in names
        journey_fks = inspector.get_foreign_keys("channel_comment_plan_contracts")
        assert any(fk["name"] == "fk_comment_plan_source_journey" for fk in journey_fks)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT current_schema()")) == "public"
        assert connection.scalar(select(Base.metadata.tables["tenants"].c.id).where(
            Base.metadata.tables["tenants"].c.id == 901)) is None


def _assert_channel_engine_schema(inspector):
    from app.models import AlbumReactionParticipation, ChannelSourceDecision, ChannelSourcePageCursor, ChannelTaskIntake
    for model in (AlbumReactionParticipation, ChannelSourceDecision, ChannelSourcePageCursor, ChannelTaskIntake):
        actual = {c["name"]: c["nullable"] for c in inspector.get_columns(model.__tablename__)}
        assert actual == {c.name: c.nullable for c in model.__table__.columns}
        assert inspector.get_foreign_keys(model.__tablename__)
        assert inspector.get_pk_constraint(model.__tablename__)["constrained_columns"]


def test_0218_upgrade_keeps_old_binding_and_accepts_unapproved_new_job(upgrade_database, monkeypatch):
    from app.models import ExecutionResiliencePolicyRevision, GenerationTimingBinding, Tenant
    from app.services.task_center import generation_timing_binding
    from tests.test_generation_timing_binding import NOW, _approve, _bind, _job

    _upgrade("0218_provider_lineage")
    monkeypatch.setattr(generation_timing_binding, "_now", lambda: NOW)
    with Session(upgrade_database) as session:
        session.add(Tenant(id=1, name="QA old binding"))
        session.flush()
        policy = ExecutionResiliencePolicyRevision(tenant_id=1, effective_from=NOW)
        session.add(policy)
        session.flush()
        task, job = _job(session, identity="legacy-binding")
        profile, _ = _approve(session, task, job)
        session.add(GenerationTimingBinding(
            generation_job_id=job.id, tenant_id=1, task_id=task.id, task_lifecycle_epoch=1,
            adapter=task.type, lane="response", execution_path_hash="a" * 64,
            timing_profile_id=profile.id, profile_snapshot_hash="b" * 64, resilience_policy_id=policy.id,
            llm_timeout_ceiling_seconds=15, bound_send_deadline_at=job.latest_safe_send_at, bound_at=NOW,
        ))
        profile_id, policy_id = profile.id, policy.id
        session.commit()
    _upgrade("head")
    with Session(upgrade_database) as session:
        old = session.get(GenerationTimingBinding, "legacy-binding")
        assert (old.timing_profile_id, old.resilience_policy_id) == (profile_id, policy_id)
        assert old.profile_snapshot_hash == "b" * 64
        task, job = _job(session, adapter="channel_comment", identity="no-profile-comment")
        assert _bind(session, task, job)["provider_calls_allowed"] is True
        assert session.get(GenerationTimingBinding, job.id).timing_profile_id is None
        session.commit()
