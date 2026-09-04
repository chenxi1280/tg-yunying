from dataclasses import replace
from datetime import timedelta

import pytest

from app.services.task_center.engagement_timing_measurements import record_execution_timing_sample
from app.services.task_center.engagement_timing_profiles import (
    ExecutionTimingProfileUnproven, derive_execution_deadline,
    publish_execution_timing_profile, require_execution_timing_profile,
)
from tests.test_engagement_timing_profiles import PATH, START, _approval, _spec, session  # noqa: F401


pytestmark = pytest.mark.no_postgres


def test_different_provider_routes_have_independent_profiles(session):
    first = record_execution_timing_sample(session, _spec())
    profile = publish_execution_timing_profile(session, _approval([first]))
    other_path = replace(PATH, provider_routes=(("realizer", "slow-route-v1"),))
    with pytest.raises(ExecutionTimingProfileUnproven, match="approved_current_profile_missing"):
        require_execution_timing_profile(session, 1, adapter="group_ai_chat", lane="response",
            at=START + timedelta(hours=1), execution_path=other_path)
    other_sample = record_execution_timing_sample(session, replace(_spec(2), execution_path=other_path))
    other = publish_execution_timing_profile(session, replace(_approval([other_sample]), execution_path=other_path))
    assert other.profile_revision == profile.profile_revision == 1
    assert other.supersedes_profile_id is None
    assert profile.state == other.state == "active"


def test_approval_cannot_mix_routes_or_preparation_policies(session):
    sample = record_execution_timing_sample(session, _spec())
    for other_path in (
        replace(PATH, provider_routes=(("realizer", "other-route"),)),
        replace(PATH, preparation_policy_revision="different-policy"),
    ):
        with pytest.raises(ExecutionTimingProfileUnproven, match="sample_scope_or_identity_mismatch"):
            publish_execution_timing_profile(session, replace(_approval([sample]), execution_path=other_path))


def test_subpaths_use_joint_measurements_for_classification_and_ready_tail(session):
    samples = [record_execution_timing_sample(session, _spec(i, before_provider_ms=1000 if i < 10 else 9000))
               for i in range(20)]
    profile = publish_execution_timing_profile(session, _approval(samples))
    assert profile.joint_path_p95_ms["pre_materialization"]["ready_action"] == 10000
    derived = derive_execution_deadline(profile, START + timedelta(minutes=2),
        path_start_stage="pre_materialization", path_end_stage="ready_action", derived_at=START)
    assert derived.protected_slack_ms == 15000
    assert derived.path_end_stage == "ready_action"
    with pytest.raises(ExecutionTimingProfileUnproven):
        derive_execution_deadline(profile, START, path_start_stage="ready_action",
            path_end_stage="pre_provider", derived_at=START)


@pytest.mark.parametrize("changes, reason", [
    ({"provider_routes": ()}, "provider_roles_invalid"),
    ({"provider_routes": (("realizer", "a"), ("realizer", "b"))}, "provider_roles_invalid"),
    ({"provider_routes": (("realizer", " "),)}, "path_revision_missing"),
    ({"measurement_revision": "unknown"}, "measurement_revision_unsupported"),
])
def test_incomplete_or_unknown_execution_paths_are_not_measured(session, changes, reason):
    with pytest.raises(ValueError, match=reason):
        record_execution_timing_sample(session, replace(_spec(), execution_path=replace(PATH, **changes)))


@pytest.mark.parametrize("adapter,lane,roles,stages", [
    ("channel_view", "passive", (), ("pre_materialization", "ready_action", "gateway_call_issued")),
    ("channel_like", "passive", (), ("pre_materialization", "ready_action", "gateway_call_issued")),
    ("group_ai_chat", "classification", (("classification", "classifier-v1"),),
     ("pre_materialization", "pre_provider", "post_classification", "claim_finalized")),
])
def test_passive_and_classification_paths_keep_their_own_stage_contract(session, *, adapter, lane, roles, stages):
    path = replace(PATH, provider_routes=roles)
    spec = replace(_spec(), adapter=adapter, lane=lane, execution_path=path,
        boundaries={stage: START + timedelta(seconds=index) for index, stage in enumerate(stages)})
    sample = record_execution_timing_sample(session, spec)
    profile = publish_execution_timing_profile(session, replace(_approval([sample]), adapter=adapter, lane=lane, execution_path=path))
    assert profile.joint_path_p95_ms[stages[0]][stages[-1]] == (len(stages) - 1) * 1000
    assert profile.execution_path["provider_routes"] == dict(roles)


def test_same_evidence_cannot_be_relabelled_to_a_different_route(session):
    record_execution_timing_sample(session, _spec())
    with pytest.raises(ValueError, match="replay_conflict"):
        record_execution_timing_sample(session, replace(_spec(),
            execution_path=replace(PATH, provider_routes=(("realizer", "different-route"),))))


def test_group_repair_route_changes_path_and_cannot_reuse_primary_only_samples(session):
    sample = record_execution_timing_sample(session, _spec())
    repair_path = replace(PATH, provider_routes=(*PATH.provider_routes, ("repair", "repair-route-v2")))
    with pytest.raises(ExecutionTimingProfileUnproven, match="sample_scope_or_identity_mismatch"):
        publish_execution_timing_profile(session, replace(_approval([sample]), execution_path=repair_path))
    reordered = replace(repair_path, provider_routes=tuple(reversed(repair_path.provider_routes)))
    assert repair_path.snapshot(adapter="group_ai_chat", lane="response") == reordered.snapshot(adapter="group_ai_chat", lane="response")


def test_one_execution_evidence_cannot_be_counted_in_two_lanes(session):
    record_execution_timing_sample(session, _spec())
    with pytest.raises(ValueError, match="replay_conflict"):
        record_execution_timing_sample(session, replace(_spec(), lane="proactive"))
