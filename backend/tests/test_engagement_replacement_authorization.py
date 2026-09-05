from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app.models import AdultSubjectAttestation, Task
from app.schemas import GroupAIChatTaskCreate
from app.services.task_center.ai_content_policy import (
    TaskBindingSpec, assert_route_authorized, bind_task_policy, create_adult_attestation,
)
from app.services.task_center.engagement_replacement_config import replacement_authorizations
from tests.test_ai_content_policy import _attestation_spec, _engine, _policy, _seed


pytestmark = pytest.mark.no_postgres


def _authorization(session):
    task = _seed(session)
    policy = _policy(session)
    attestation = create_adult_attestation(session, _attestation_spec(scope_id="7"))
    task.type_config = {"ai_content_attestation_ids": [attestation.id]}
    payload = GroupAIChatTaskCreate(name="替代活群", target_group_id=7, topic_participation_rate=0.05,
        ai_content_attestation_ids=[attestation.id], ai_two_stage_enabled=True,
        ai_content_route_v2_enabled=True, ai_content_policy_version_id=policy.id,
        ai_content_allowed_routes=["adult_service_sensory"], ai_model="generator-v1",
        ai_semantic_reviewer_model="reviewer-v1")
    return task, policy, attestation, payload


def test_replacement_authorization_keeps_original_scope_actor_evidence_and_expiration():
    with Session(_engine()) as session:
        old, policy, original, payload = _authorization(session)
        replacement, mapping = replacement_authorizations(session, old, payload)
        cloned = session.get(AdultSubjectAttestation, mapping[original.id])
        fields = ("scope_type", "scope_id", "subject_class", "actor_user_id", "evidence_codes",
            "permission_snapshot", "attested_at", "expires_at", "policy_version")
        assert all(getattr(cloned, key) == getattr(original, key) for key in fields)
        assert cloned.task_config_revision == 1 and original.task_config_revision == 3
        new = Task(tenant_id=1, type=old.type, name="替代活群", config_revision=1)
        session.add(new)
        session.flush()
        binding = bind_task_policy(session, TaskBindingSpec(task_id=new.id, policy_version_id=policy.id,
            allowed_routes=("adult_service_sensory",), attestation_ids=tuple(replacement.ai_content_attestation_ids),
            scope_refs=(("task_group", "7"),), approved_by=policy.approved_by))
        assert_route_authorized(session, binding, route="adult_service_sensory",
            scope_type="task_group", scope_id="7")


@pytest.mark.parametrize("change", ["revoked", "expired", "revision", "scope"])
def test_replacement_does_not_reactivate_changed_or_expired_authorization(change):
    with Session(_engine()) as session:
        old, _, original, payload = _authorization(session)
        if change == "revoked":
            original.status = "revoked"
        elif change == "expired":
            original.expires_at -= timedelta(days=2)
        elif change == "revision":
            original.task_config_revision += 1
        else:
            payload = payload.model_copy(update={"ai_content_attestation_ids": ["unapproved"]})
        session.flush()
        with pytest.raises(ValueError, match="authorization|expiry"):
            replacement_authorizations(session, old, payload)
