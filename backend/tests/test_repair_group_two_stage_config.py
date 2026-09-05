from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.models import AuditLog, Task, TaskAiContentPolicyBinding
from app.services.task_center.ai_v2_canary_bootstrap import (
    apply_bootstrap, parse_choices, preview_bootstrap,
)
from scripts import repair_group_two_stage_config as script
from tests.test_ai_v2_canary_bootstrap import _choices, _engine, _seed


pytestmark = pytest.mark.no_postgres


@pytest.fixture()
def repair_scope(monkeypatch):
    monkeypatch.setenv("RELEASE_SHA", "a" * 40)
    engine = _engine()
    factory = sessionmaker(engine, expire_on_commit=False, autoflush=False)
    with factory() as session:
        _seed(session)
        choices = parse_choices(_choices())
        preview = preview_bootstrap(session, 1, choices)
        apply_bootstrap(session, 1, choices, expected_fingerprint=preview["fingerprint"])
        task = session.get(Task, "task-canary")
        task.status = "running"
        task.type_config = {**task.type_config, "ai_two_stage_enabled": False}
        session.commit()
    options = script.RepairOptions(deployed_sha="a" * 40, task_ids=("task-canary",),
        actor="test-operator", approval_ref="test-quality-repair")
    yield factory, options
    engine.dispose()


def test_repair_changes_only_stage_flag_revision_and_adds_policy_binding(repair_scope):
    factory, options = repair_scope
    with factory() as session:
        task = session.get(Task, "task-canary")
        config, accounts = dict(task.type_config), dict(task.account_config)
        original_revision, epoch = task.config_revision, task.task_lifecycle_epoch
        original = session.scalar(select(TaskAiContentPolicyBinding))
        original_id, original_hash = original.id, original.evidence_hash
    preview = script.run(options, factory)
    result = script.run(replace(options, apply=True, expected_hash=preview["fingerprint"]), factory)

    assert result["applied"] is True
    with factory() as session:
        task = session.get(Task, "task-canary")
        assert task.type_config == {**config, "ai_two_stage_enabled": True}
        assert task.account_config == accounts
        assert task.config_revision == original_revision + 1
        assert task.task_lifecycle_epoch == epoch
        assert session.get(TaskAiContentPolicyBinding, original_id).evidence_hash == original_hash
        assert len(list(session.scalars(select(TaskAiContentPolicyBinding)))) == 2


def test_preview_changes_no_persisted_configuration_or_binding(repair_scope):
    factory, options = repair_scope
    preview = script.run(options, factory)

    assert preview["tasks"][0]["changes"] == {"ai_two_stage_enabled": {"old": False, "new": True}}
    with factory() as session:
        assert session.get(Task, "task-canary").type_config["ai_two_stage_enabled"] is False
        assert len(list(session.scalars(select(TaskAiContentPolicyBinding)))) == 1


def test_config_drift_aborts_without_extra_binding(repair_scope):
    factory, options = repair_scope
    preview = script.run(options, factory)
    with factory() as session:
        task = session.get(Task, "task-canary")
        task.type_config = {**task.type_config, "daily_message_target": 2000}
        session.commit()
    with pytest.raises(RuntimeError, match="group_two_stage_preview_drift"):
        script.run(replace(options, apply=True, expected_hash=preview["fingerprint"]), factory)
    with factory() as session:
        assert session.get(Task, "task-canary").type_config["ai_two_stage_enabled"] is False
        assert len(list(session.scalars(select(TaskAiContentPolicyBinding)))) == 1
        assert list(session.scalars(select(AuditLog).where(
            AuditLog.action == "repair_group_two_stage_config"))) == []


def test_restores_general_binding_when_task_has_other_groups_fields(repair_scope):
    factory, options = repair_scope
    with factory() as session:
        task = session.get(Task, "task-canary")
        task.type_config = {**task.type_config, "ai_two_stage_enabled": True,
            "ai_content_allowed_routes": ["general", "adult_visual"],
            "ai_content_attestation_ids": ["wrong-group-expired"]}
        session.commit()
    preview = script.run(options, factory)
    script.run(replace(options, apply=True, expected_hash=preview["fingerprint"]), factory)
    with factory() as session:
        config = session.get(Task, "task-canary").type_config
        assert config["ai_content_allowed_routes"] == ["general"]
        assert config["ai_content_attestation_ids"] == []
        assert config["ai_two_stage_enabled"] is True


@pytest.fixture()
def adult_repair_scope(monkeypatch):
    from datetime import datetime, timedelta
    from app.services.task_center.ai_content_policy import AttestationSpec, create_adult_attestation

    monkeypatch.setenv("RELEASE_SHA", "a" * 40)
    engine = _engine()
    factory = sessionmaker(engine, expire_on_commit=False, autoflush=False)
    with factory() as session:
        _seed(session)
        source = create_adult_attestation(session, AttestationSpec(
            tenant_id=1, scope_type="task_group", scope_id="7", subject_class="adult_visual",
            evidence_codes=("adult_visual_content_verified",), actor_user_id=1,
            permission_snapshot={"adult_content_attest": True},
            expires_at=datetime.now() + timedelta(days=7), task_config_revision=4, policy_version=1))
        payload = _choices()
        payload["allowed_routes"] = ["general", "adult_visual"]
        payload["attestation_ids"] = [source.id]
        payload["route_items"]["group_realize_adult_visual"] = payload["route_items"]["group_realize_general"]
        choices = parse_choices(payload)
        preview = preview_bootstrap(session, 1, choices)
        apply_bootstrap(session, 1, choices, expected_fingerprint=preview["fingerprint"])
        task = session.get(Task, "task-canary")
        task.status = "running"
        task.type_config = {**task.type_config, "ai_two_stage_enabled": False,
            "ai_content_allowed_routes": ["general"], "ai_content_attestation_ids": ["bad-source"]}
        session.commit()
        source_id = source.id
    options = script.RepairOptions(deployed_sha="a" * 40, task_ids=("task-canary",),
        actor="test-operator", approval_ref="test-quality-repair")
    yield factory, options, source_id
    engine.dispose()


def test_authorization_successor_preserves_original_scope_and_expiry(adult_repair_scope):
    from app.models import AdultSubjectAttestation

    factory, options, source_id = adult_repair_scope
    with factory() as session:
        source_hash = script.row_hash(session.get(AdultSubjectAttestation, source_id))
    preview = script.run(options, factory)
    script.run(replace(options, apply=True, expected_hash=preview["fingerprint"]), factory)
    with factory() as session:
        source = session.get(AdultSubjectAttestation, source_id)
        task = session.get(Task, "task-canary")
        successor = session.get(AdultSubjectAttestation, task.type_config["ai_content_attestation_ids"][0])
        assert script.row_hash(source) == source_hash
        assert source.id != successor.id
        assert successor.task_config_revision == task.config_revision == source.task_config_revision + 1
        for field in ("tenant_id", "scope_type", "scope_id", "subject_class", "actor_user_id",
                      "permission_snapshot", "evidence_codes", "attested_at", "expires_at", "policy_version"):
            assert getattr(successor, field) == getattr(source, field)
        assert task.type_config["ai_content_allowed_routes"] == ["general", "adult_visual"]
        assert session.get(TaskAiContentPolicyBinding, preview["tasks"][0]["old_binding_id"]).attestation_ids == [source_id]


def test_expired_original_authorization_prevents_successor(adult_repair_scope):
    from datetime import datetime, timedelta
    from app.models import AdultSubjectAttestation

    factory, options, source_id = adult_repair_scope
    with factory() as session:
        session.get(AdultSubjectAttestation, source_id).expires_at = datetime.now() - timedelta(days=1)
        session.commit()
    with pytest.raises(ValueError, match="adult_attestation_expiry_invalid"):
        script.run(options, factory)
    with factory() as session:
        assert len(list(session.scalars(select(AdultSubjectAttestation)))) == 1


def test_authorization_drift_aborts_before_any_successor(adult_repair_scope):
    from app.models import AdultSubjectAttestation

    factory, options, source_id = adult_repair_scope
    preview = script.run(options, factory)
    with factory() as session:
        session.get(AdultSubjectAttestation, source_id).permission_snapshot = {
            "adult_content_attest": True, "revision": 2}
        session.commit()
    with pytest.raises(RuntimeError, match="group_two_stage_preview_drift"):
        script.run(replace(options, apply=True, expected_hash=preview["fingerprint"]), factory)
    with factory() as session:
        assert len(list(session.scalars(select(AdultSubjectAttestation)))) == 1
        assert session.get(Task, "task-canary").config_revision == 4


def test_new_binding_failure_rolls_back_task_and_authorization(adult_repair_scope, monkeypatch):
    from app.models import AdultSubjectAttestation

    factory, options, _source_id = adult_repair_scope
    preview = script.run(options, factory)

    def fail_binding(_session, _task):
        raise RuntimeError("injected_binding_failure")

    monkeypatch.setattr(script, "activate_task_ai_content_config", fail_binding)
    with pytest.raises(RuntimeError, match="injected_binding_failure"):
        script.run(replace(options, apply=True, expected_hash=preview["fingerprint"]), factory)
    with factory() as session:
        assert len(list(session.scalars(select(AdultSubjectAttestation)))) == 1
        assert session.get(Task, "task-canary").config_revision == 4
        assert len(list(session.scalars(select(TaskAiContentPolicyBinding)))) == 1


def test_reports_old_binding_hash_defect_and_validates_new_revision(repair_scope):
    factory, options = repair_scope
    with factory() as session:
        old = session.scalar(select(TaskAiContentPolicyBinding))
        old.evidence_hash = "historical-incorrect-digest"
        old_id = old.id
        session.commit()
    preview = script.run(options, factory)
    assert preview["tasks"][0]["original_binding_evidence_matches_fields"] is False
    result = script.run(replace(options, apply=True, expected_hash=preview["fingerprint"]), factory)
    with factory() as session:
        assert session.get(TaskAiContentPolicyBinding, old_id).evidence_hash == "historical-incorrect-digest"
        new = session.get(TaskAiContentPolicyBinding, result["after"][0]["binding_id"])
        assert new.evidence_hash == preview["tasks"][0]["next_binding_evidence_hash"]
        assert new.evidence_hash != "historical-incorrect-digest"


def test_corrected_authorized_task_cannot_rotate_revision_again(adult_repair_scope):
    factory, options, _source_id = adult_repair_scope
    preview = script.run(options, factory)
    script.run(replace(options, apply=True, expected_hash=preview["fingerprint"]), factory)
    with pytest.raises(RuntimeError, match="group_two_stage_repair_not_required"):
        script.run(options, factory)
