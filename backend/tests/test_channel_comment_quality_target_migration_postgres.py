from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import sessionmaker

from app.models import (
    ChannelCommentGroundingAssignment,
    ChannelCommentPlanContract,
    ChannelCommentQualityTargetRevision,
)
import test_channel_comment_capacity_postgres as capacity_fixture
from test_channel_comment_capacity_postgres import (
    PLAN_ID,
    _cleanup,
    _seed_scope,
)
from tests.test_engagement_upgrade_postgres import upgrade_database


pytestmark = pytest.mark.allow_missing_rule_binding

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_0192_backfills_legacy_quality_target_and_round_trips(upgrade_database, monkeypatch) -> None:
    database = upgrade_database
    monkeypatch.setattr(capacity_fixture, "SessionLocal", sessionmaker(bind=database))
    _migrate(database, "upgrade", "head")
    _cleanup()
    _seed_scope()
    try:
        _migrate(database, "downgrade", "0191_comment_source_delete")
        assert "current_quality_target_revision_id" not in _columns(database,
            "channel_comment_plan_contracts",
        )
        _migrate(database, "upgrade", "head")
        assert _scalar(database, text("select version_num from alembic_version")) == (
            "0225_account_group_revisions"
        )
        assert _scalar(database, select(func.count()).select_from(
            ChannelCommentPlanContract,
        ).where(
            ChannelCommentPlanContract.id == PLAN_ID,
            ChannelCommentPlanContract.initial_quality_target_revision_id.is_not(None),
            ChannelCommentPlanContract.current_quality_target_revision_id
            == ChannelCommentPlanContract.initial_quality_target_revision_id,
        )) == 1
        assert _scalar(database, select(func.count()).select_from(
            ChannelCommentQualityTargetRevision,
        ).where(
            ChannelCommentQualityTargetRevision.plan_contract_id == PLAN_ID,
            ChannelCommentQualityTargetRevision.quality_target_revision == 1,
        )) == 1
        assert _scalar(database, select(func.count()).select_from(
            ChannelCommentGroundingAssignment,
        ).where(
            ChannelCommentGroundingAssignment.plan_contract_id == PLAN_ID,
            ChannelCommentGroundingAssignment.quality_target_revision_id.is_not(None),
        )) == 2
        assert "fk_channel_comment_assignment_quality_target" in _foreign_keys(database,
            "channel_comment_grounding_assignments",
        )
        assert "fk_channel_comment_quality_target_supersedes" in _foreign_keys(database,
            "channel_comment_quality_target_revisions",
        )
    finally:
        _migrate(database, "upgrade", "head")
        _cleanup()


def _migrate(database, operation: str, revision: str) -> None:
    database.dispose()
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    getattr(command, operation)(config, revision)
    database.dispose()


def _columns(database, table_name: str) -> set[str]:
    return {row["name"] for row in inspect(database).get_columns(table_name)}


def _foreign_keys(database, table_name: str) -> set[str]:
    return {
        str(row["name"] or "")
        for row in inspect(database).get_foreign_keys(table_name)
    }


def _scalar(database, statement):
    with database.connect() as connection:
        return connection.execute(statement).scalar_one()
