"""A non-content revision preserves approved scope and immutable old bindings."""
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AdultSubjectAttestation, AiContentPolicyVersion, Task, TaskAiContentPolicyBinding, Tenant
from app.services._common import _now
from app.services.task_center import ai_content_binding_revision as revision
from app.services.task_center.ai_content_policy import AttestationSpec, TaskBindingSpec, bind_task_policy, create_adult_attestation
from tests.owner_postgres_support import owner_engine

pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]


@pytest.fixture
def binding_session(owner_engine, monkeypatch):
    monkeypatch.setattr(revision, "_validate_provider_routes", lambda *_: None)
    with Session(owner_engine) as session:
        session.add(Tenant(id=1, name="test"))
        session.flush()
        yield session


def seed(session, *, adult=False):
    routes = ["general", "adult_visual"] if adult else ["general"]
    policy = AiContentPolicyVersion(id="policy", tenant_id=1, version=1, status="active",
        policy_hash="p" * 64, approved_by="existing-approver", route_rules={"allowed_routes": routes})
    task = Task(id="task", tenant_id=1, name="test", type="group_ai_chat", status="running",
        config_revision=1, task_lifecycle_epoch=2, type_config={"ai_content_route_v2_enabled": True,
            "ai_two_stage_enabled": True, "ai_content_policy_version_id": policy.id,
            "ai_content_allowed_routes": routes, "ai_content_attestation_ids": [], "target_group_id": 7})
    session.add_all([policy, task])
    session.flush()
    ids = []
    if adult:
        attestation = create_adult_attestation(session, AttestationSpec(tenant_id=1,
            scope_type="task_group", scope_id="7", subject_class="adult_visual",
            evidence_codes=("adult_visual_content_verified",), actor_user_id=None,
            permission_snapshot={"adult_content_attest": True}, expires_at=_now()+timedelta(days=1),
            task_config_revision=1, policy_version=1))
        ids = [attestation.id]
        task.type_config = {**task.type_config, "ai_content_attestation_ids": ids}
    binding = bind_task_policy(session, TaskBindingSpec(task_id=task.id, policy_version_id=policy.id,
        allowed_routes=tuple(routes), attestation_ids=tuple(ids),
        scope_refs=(("task_group", "7"),) if adult else (), approved_by=policy.approved_by))
    task.config_revision = 2
    task.group_ai_prejoin_channel_ids = ["required-channel"]
    session.commit()
    return task, binding


@pytest.mark.parametrize("adult", [False, True])
def test_binding_successor_keeps_scope_and_old_evidence(binding_session, adult):
    session = binding_session
    task, original = seed(session, adult=adult)
    original_hash = original.evidence_hash
    preview = revision.preview_content_binding_revision(session, task.id, source_revision=1)
    assert len(session.scalars(select(TaskAiContentPolicyBinding)).all()) == 1
    result = revision.apply_content_binding_revision(session, preview, actor="operator", approval_reference="test")
    session.commit()
    session.expire_all()
    successor = session.get(TaskAiContentPolicyBinding, result["binding_id"])
    assert successor.task_config_revision == 2 and original.task_config_revision == 1
    assert successor.allowed_routes == original.allowed_routes
    assert successor.approved_by == original.approved_by
    assert original.evidence_hash == original_hash
    if adult:
        old = session.get(AdultSubjectAttestation, original.attestation_ids[0])
        new = session.get(AdultSubjectAttestation, successor.attestation_ids[0])
        assert new.id != old.id and new.task_config_revision == 2
        assert (new.expires_at, new.attested_at, new.permission_snapshot) == (
            old.expires_at, old.attested_at, old.permission_snapshot)
        assert new.scope_id == old.scope_id == "7"
    with pytest.raises(ValueError, match="already_present"):
        revision.apply_content_binding_revision(session, preview, actor="operator", approval_reference="test")


def test_drift_rejected_without_successors(binding_session):
    session = binding_session
    task, _ = seed(session)
    preview = revision.preview_content_binding_revision(session, task.id, source_revision=1)
    task.group_ai_prejoin_channel_ids = ["other-channel"]
    with pytest.raises(RuntimeError, match="fingerprint_drift"):
        revision.apply_content_binding_revision(session, preview, actor="operator", approval_reference="test")
    session.rollback()
    assert len(session.scalars(select(TaskAiContentPolicyBinding)).all()) == 1


@pytest.mark.parametrize("field,value,error", [
    ("ai_content_allowed_routes", ["adult_visual"], "authority_changed"),
    ("target_group_id", 8, "scope_missing"),
])
def test_cannot_extend_authority(binding_session, field, value, error):
    task, _ = seed(binding_session, adult=True)
    task.type_config = {**task.type_config, field: value}
    with pytest.raises((ValueError, RuntimeError), match=error):
        revision.preview_content_binding_revision(binding_session, task.id, source_revision=1)
    binding_session.rollback()


def test_revoked_evidence_cannot_be_carried(binding_session):
    task, binding = seed(binding_session, adult=True)
    binding_session.get(AdultSubjectAttestation, binding.attestation_ids[0]).status = "revoked"
    with pytest.raises(RuntimeError, match="scope_missing"):
        revision.preview_content_binding_revision(binding_session, task.id, source_revision=1)


def test_reference_restoration_is_explicit_and_uses_original_authority(binding_session):
    session = binding_session
    task, original = seed(session, adult=True)
    task.type_config = {**task.type_config, "ai_content_attestation_ids": ["wrong-scope-reference"]}
    session.commit()
    with pytest.raises(ValueError, match="authority_changed"):
        revision.preview_content_binding_revision(session, task.id, source_revision=1)
    preview = revision.preview_content_binding_revision(session, task.id, source_revision=1,
        restore_original_references=True)
    result = revision.apply_content_binding_revision(session, preview, actor="operator", approval_reference="test")
    session.commit()
    new = session.get(TaskAiContentPolicyBinding, result["binding_id"])
    assert new.attestation_ids == task.type_config["ai_content_attestation_ids"]
    assert new.attestation_ids != ["wrong-scope-reference"]
    assert original.attestation_ids != new.attestation_ids


def test_cli_manifest_apply_readback_and_tampering(binding_session):
    from types import SimpleNamespace
    from scripts import repair_ai_group_policy_revision as repair

    task, _ = seed(binding_session)
    sha = "a" * 40
    spec = {"task_ids": [task.id], "expected_count": 1, "tenant_id": task.tenant_id}
    document = repair.preview(binding_session, spec, sha)
    options = SimpleNamespace(expected_deployed_sha=sha, actor="operator", approval_reference="test")
    with pytest.raises(RuntimeError, match="manifest_invalid"):
        repair.apply(binding_session, {**document, "fingerprint": "wrong"}, options)
    receipt = repair.apply(binding_session, document, options)
    binding_session.commit()
    assert repair.readback(binding_session, receipt, sha)["status"] == "persisted_verified"


@pytest.mark.parametrize("endpoint", ["settings", "group_config"])
@pytest.mark.parametrize("adult", [False, True])
def test_prejoin_api_commits_final_revision_binding(binding_session, monkeypatch, endpoint, adult):
    from app.schemas import GroupAIChatTaskConfigUpdate, TaskSettingsUpdate
    from app.services.task_center import service, task_ai_content_activation

    session = binding_session
    task, original = seed(session, adult=adult)
    task.type_config = {**task.type_config, "ai_model": "generator",
        "ai_semantic_reviewer_model": "reviewer", "topic_participation_rate": 0.10}
    service._apply_validated_type_config(session, task, {}, remove_fields=())
    task.config_revision = 1
    task.group_ai_prejoin_channel_ids = []
    session.commit()
    monkeypatch.setattr(task_ai_content_activation, "_validate_provider_routes", lambda *_: None)
    if endpoint == "settings":
        service.update_task_settings(session, 1, task.id,
            TaskSettingsUpdate(group_ai_prejoin_channel_ids=["new_channel"]), actor="operator")
    else:
        service.update_group_ai_chat_config(session, 1, task.id,
            GroupAIChatTaskConfigUpdate(target_group_id=7, topic_participation_rate=0.10,
                group_ai_prejoin_channel_ids=["new_channel"]), actor="operator")
    session.expire_all()
    assert task.config_revision == 2
    bindings = session.scalars(select(TaskAiContentPolicyBinding).where(
        TaskAiContentPolicyBinding.task_id == task.id)).all()
    assert {item.task_config_revision for item in bindings} == {1, 2}
    assert original.evidence_hash
