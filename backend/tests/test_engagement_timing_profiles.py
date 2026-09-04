from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Tenant
from app.services.task_center.engagement_timing_path import TimingExecutionPath
from app.services.task_center.engagement_timing_measurements import (
    TimingSampleInput, nearest_rank_p95, record_execution_timing_sample, safety_margin_ms,
)
from app.services.task_center.engagement_timing_profiles import (
    ExecutionTimingProfileUnproven, TimingProfileApproval, derive_execution_deadline,
    publish_execution_timing_profile, require_execution_timing_profile,
)


pytestmark = pytest.mark.no_postgres
START = datetime(2026, 9, 4, 12)
PATH = TimingExecutionPath("test-preparation-v1", (("realizer", "test-route-v1"),))


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as current:
        current.add(Tenant(id=1, name="timing QA"))
        current.flush()
        yield current


def _spec(index=1, *, before_provider_ms=1000):
    return TimingSampleInput(
        tenant_id=1, adapter="group_ai_chat", lane="response", evidence_kind="shadow_run",
        evidence_reference=f"measured-shadow-{index}", evidence_hash=f"{index:064x}",
        execution_path=PATH,
        boundaries={
            "pre_materialization": START,
            "pre_provider": START + timedelta(milliseconds=before_provider_ms),
            "ready_action": START + timedelta(seconds=10),
            "gateway_call_issued": START + timedelta(seconds=11),
        },
    )


def _approval(samples, *, effective_at=None, minimum=1):
    return TimingProfileApproval(
        tenant_id=1, adapter="group_ai_chat", lane="response",
        sample_ids=tuple(row.id for row in samples), minimum_sample_count=minimum,
        approved_by="QA fixture approver", approval_reference="test-only-calibration-review",
        effective_at=effective_at or START + timedelta(minutes=1),
        valid_until=START + timedelta(days=1),
        execution_path=PATH,
    )


def test_profile_uses_complete_sample_paths_not_sum_of_stage_percentiles(session):
    samples = [record_execution_timing_sample(session, _spec(index, before_provider_ms=1000 if index < 10 else 9000))
               for index in range(20)]
    profile = publish_execution_timing_profile(session, _approval(samples, minimum=20))
    assert profile.sample_count == 20
    assert sum(profile.stage_p95_ms.values()) == 19000
    assert profile.remaining_path_p95_ms["pre_materialization"] == 11000
    timing = derive_execution_deadline(
        profile, START + timedelta(minutes=2), path_start_stage="pre_materialization", derived_at=START,
    )
    assert timing.protected_slack_ms == 16000
    assert timing.latest_start_at == START + timedelta(seconds=104)
    assert timing.profile_id == profile.id
    assert timing.path_start_stage == "pre_materialization"


def test_sample_replay_is_idempotent_but_changed_measurement_is_not_overwritten(session):
    first = record_execution_timing_sample(session, _spec())
    assert record_execution_timing_sample(session, _spec()).id == first.id
    with pytest.raises(ValueError, match="replay_conflict"):
        record_execution_timing_sample(session, _spec(before_provider_ms=2000))
    assert first.remaining_path_ms["pre_provider"] == 10000


def test_comment_sample_requires_reviewer_in_the_measured_path(session):
    comment_path = replace(PATH, provider_routes=(*PATH.provider_routes, ("reviewer", "test-reviewer-v1")))
    spec = replace(_spec(), adapter="channel_comment", execution_path=comment_path)
    with pytest.raises(ValueError, match="path_incomplete"):
        record_execution_timing_sample(session, spec)
    spec = replace(spec, boundaries={**spec.boundaries, "reviewer_started": START + timedelta(seconds=7)})
    sample = record_execution_timing_sample(session, spec)
    assert sample.remaining_path_ms["pre_provider"] == 10000
    assert sample.stage_durations_ms["reviewer_started"] == 3000


def test_shadow_evidence_is_not_automatically_approved(session):
    sample = record_execution_timing_sample(session, _spec())
    with pytest.raises(ExecutionTimingProfileUnproven, match="approved_current_profile_missing"):
        require_execution_timing_profile(session, 1, adapter="group_ai_chat", lane="response", at=START, execution_path=PATH)
    with pytest.raises(ExecutionTimingProfileUnproven, match="approval_missing"):
        publish_execution_timing_profile(session, replace(_approval([sample]), approval_reference=""))
    with pytest.raises(ExecutionTimingProfileUnproven, match="sample_count_insufficient"):
        publish_execution_timing_profile(session, _approval([sample], minimum=2))


def test_successor_preserves_frozen_revision_and_does_not_activate_early(session):
    sample = record_execution_timing_sample(session, _spec())
    approval = _approval([sample])
    first = publish_execution_timing_profile(session, approval)
    assert publish_execution_timing_profile(session, approval).id == first.id
    second = publish_execution_timing_profile(session, replace(approval, effective_at=START + timedelta(hours=1)))
    assert second.profile_revision == 2
    assert second.supersedes_profile_id == first.id
    assert require_execution_timing_profile(session, 1, adapter="group_ai_chat", lane="response", at=START + timedelta(minutes=5), execution_path=PATH).id == first.id
    assert require_execution_timing_profile(session, 1, adapter="group_ai_chat", lane="response", at=START + timedelta(hours=2), execution_path=PATH).id == second.id
    old = derive_execution_deadline(first, START + timedelta(hours=2), path_start_stage="ready_action", derived_at=START)
    assert old.profile_revision == 1
    assert old.complete_remaining_path_p95_ms == 1000


def test_expired_new_revision_does_not_silently_fall_back_to_older_profile(session):
    sample = record_execution_timing_sample(session, _spec())
    approval = _approval([sample])
    publish_execution_timing_profile(session, approval)
    publish_execution_timing_profile(session, replace(approval,
        effective_at=START + timedelta(hours=1), valid_until=START + timedelta(hours=2)))
    with pytest.raises(ExecutionTimingProfileUnproven, match="profile_expired"):
        require_execution_timing_profile(session, 1, adapter="group_ai_chat", lane="response", at=START + timedelta(hours=3), execution_path=PATH)


def test_missing_or_mixed_scope_samples_do_not_produce_profile(session):
    sample = record_execution_timing_sample(session, _spec())
    with pytest.raises(ExecutionTimingProfileUnproven, match="sample_scope_or_identity_mismatch"):
        publish_execution_timing_profile(session, replace(_approval([sample]), lane="proactive"))
    with pytest.raises(ValueError, match="duplicate_sample"):
        publish_execution_timing_profile(session, _approval([sample, sample]))


def test_remote_sample_must_link_to_a_real_matching_attempt(session):
    with pytest.raises(ValueError, match="attempt_missing"):
        record_execution_timing_sample(session, replace(_spec(), evidence_kind="remote_attempt"))


def test_timezones_normalize_before_sample_hash_and_duration(session):
    spec = _spec()
    first = record_execution_timing_sample(session, spec)
    utc_boundaries = {key: (value - timedelta(hours=8)).replace(tzinfo=timezone.utc) for key, value in spec.boundaries.items()}
    assert record_execution_timing_sample(session, replace(spec, boundaries=utc_boundaries)).id == first.id


def test_p95_and_safety_margin_follow_frozen_policy_units():
    assert nearest_rank_p95(list(range(1, 21))) == 19
    assert safety_margin_ms(25000) == 5000
    assert safety_margin_ms(25001) == 6000
    assert safety_margin_ms(30000) == 6000
