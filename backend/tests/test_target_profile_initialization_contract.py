from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Tenant, TenantLearningProfile, TenantLearningProfileVersion
from app.models import TenantLearningQualityRule, TenantLearningRun, TenantLearningSample, TenantLearningSource
from app.services.tenant_target_profile import get_target_profile_overview

from test_tenant_target_profile import _session


pytestmark = pytest.mark.no_postgres

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEARNING_MIGRATION = PROJECT_ROOT / "backend/migrations/versions/0053_tenant_learning_profiles.py"
EXPECTED_TABLES = {
    "tenant_learning_profiles",
    "tenant_learning_sources",
    "tenant_learning_samples",
    "tenant_learning_quality_rules",
    "tenant_learning_profile_versions",
    "tenant_learning_runs",
}


def test_first_target_profile_open_creates_single_empty_profile_version() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()

        first = get_target_profile_overview(session, 1)
        second = get_target_profile_overview(session, 1)
        profile = session.scalar(select(TenantLearningProfile).where(TenantLearningProfile.tenant_id == 1))
        versions = _profile_versions(session)

    assert first["profile_version"] == 0
    assert first["status"] == "sample_insufficient"
    assert second["profile_id"] == first["profile_id"]
    assert profile is not None
    assert len(versions) == 1
    assert versions[0].tenant_id == 1
    assert versions[0].profile_version == 0
    assert versions[0].sample_count == 0
    assert versions[0].created_by == "system"
    assert versions[0].profile_snapshot["status"] == "sample_insufficient"


def test_target_profile_models_and_migration_define_all_tenant_learning_tables() -> None:
    model_tables = {
        TenantLearningProfile.__tablename__,
        TenantLearningSource.__tablename__,
        TenantLearningSample.__tablename__,
        TenantLearningQualityRule.__tablename__,
        TenantLearningProfileVersion.__tablename__,
        TenantLearningRun.__tablename__,
    }
    migration = LEARNING_MIGRATION.read_text()

    assert model_tables == EXPECTED_TABLES
    for table in EXPECTED_TABLES:
        assert f'"{table}"' in migration


def test_target_profile_models_do_not_define_target_or_scene_identity() -> None:
    profile_columns = TenantLearningProfile.__table__.columns.keys()
    version_columns = TenantLearningProfileVersion.__table__.columns.keys()

    assert "target_id" not in profile_columns
    assert "profile_scene" not in profile_columns
    assert "target_id" not in version_columns
    assert "profile_scene" not in version_columns


def _profile_versions(session: Session) -> list[TenantLearningProfileVersion]:
    return list(session.scalars(select(TenantLearningProfileVersion).order_by(TenantLearningProfileVersion.profile_version)))
