from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import OperationTarget, Tenant, TenantLearningProfile, TenantLearningProfileVersion, TenantLearningRun, TenantLearningSample, TenantLearningSource
from app.services.tenant_target_profile import rebuild_profile, recompute_candidates, update_quality_rules


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def test_quality_rule_update_does_not_silently_recompute_candidates_or_profile() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=31, tenant_id=1, target_type="group", tg_peer_id="-10031", title="活群"))
        source = TenantLearningSource(tenant_id=1, target_id=31, source_kind="group")
        session.add(source)
        session.flush()
        session.add(TenantLearningProfile(tenant_id=1, profile_version=7, status="active", style_summary="旧画像", source_sample_count=3))
        session.add(TenantLearningSample(tenant_id=1, source_id=source.id, source_message_id="auto", text="广告文案", learning_status="accepted"))
        session.commit()

        rule_payload = update_quality_rules(
            session,
            1,
            {"forbidden_patterns": {"keywords": ["广告"], "links": True, "contacts": True}},
            actor="tester",
            reason="只保存规则",
        )
        sample = session.scalar(select(TenantLearningSample).where(TenantLearningSample.source_message_id == "auto"))
        profile = session.scalar(select(TenantLearningProfile).where(TenantLearningProfile.tenant_id == 1))
        runs_after_rule_save = session.scalars(select(TenantLearningRun)).all()
        versions_after_rule_save = session.scalars(select(TenantLearningProfileVersion)).all()
        sample_status_after_rule_save = sample.learning_status if sample else ""
        profile_version_after_rule_save = profile.profile_version if profile else 0
        profile_summary_after_rule_save = profile.style_summary if profile else ""

        recompute_candidates(session, 1, actor="tester", reason="显式重算候选")
        recomputed_sample = session.scalar(select(TenantLearningSample).where(TenantLearningSample.source_message_id == "auto"))
        status_after_recompute = recomputed_sample.learning_status if recomputed_sample else ""
        profile_after_recompute = session.scalar(select(TenantLearningProfile).where(TenantLearningProfile.tenant_id == 1))
        profile_version_after_recompute = profile_after_recompute.profile_version if profile_after_recompute else 0

        rebuilt = rebuild_profile(session, 1, actor="tester", reason="显式重建画像")
        session.commit()

    assert rule_payload["rule_version"] == 1
    assert sample_status_after_rule_save == "accepted"
    assert profile_version_after_rule_save == 7
    assert profile_summary_after_rule_save == "旧画像"
    assert runs_after_rule_save == []
    assert versions_after_rule_save == []
    assert status_after_recompute == "rejected"
    assert profile_version_after_recompute == 7
    assert rebuilt["profile_version"] == 8
    assert rebuilt["style_summary"] == ""
