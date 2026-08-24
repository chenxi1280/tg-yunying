from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AiProvider,
    AppUser,
    Task,
    TaskAiContentPolicyBinding,
    Tenant,
    TenantAiProviderRouteItem,
    TenantAiProviderRouteSet,
)
from app.services.task_center.ai_content_policy import (
    AiContentPolicyConflict,
    AttestationSpec,
    PolicyDraft,
    TaskBindingSpec,
    activate_policy,
    approve_policy,
    assert_route_authorized,
    bind_task_policy,
    create_adult_attestation,
    create_policy_draft,
)
from app.services.task_center.task_ai_content_activation import (
    activate_task_ai_content_config,
)


pytestmark = pytest.mark.no_postgres


def _engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def _seed(session: Session) -> Task:
    session.add(Tenant(id=1, name="tenant-1"))
    session.add(AppUser(
        id=9,
        tenant_id=1,
        name="reviewer",
        role="系统管理员",
        email="reviewer@example.test",
    ))
    task = Task(
        id="task-1",
        tenant_id=1,
        name="group-ai",
        type="group_ai_chat",
        config_revision=3,
        task_lifecycle_epoch=2,
    )
    session.add(task)
    session.flush()
    return task


def _policy(session: Session):
    policy = create_policy_draft(session, PolicyDraft(
        tenant_id=1,
        version=1,
        route_rules={"allowed_routes": ["general", "adult_service_sensory"]},
        prompt_registry={
            "general": {"version": "general_v1"},
            "adult_service_sensory": {"version": "adult_service_sensory_v1"},
        },
        gate_config={"semantic_reviewer_required": True},
        example_set={"version": "adult_human_anchors_v1"},
    ))
    approve_policy(session, policy.id, approved_by="approver")
    return activate_policy(session, policy.id)


def _attestation_spec(*, scope_id: str = "group-7", revision: int = 3) -> AttestationSpec:
    return AttestationSpec(
        tenant_id=1,
        scope_type="task_group",
        scope_id=scope_id,
        subject_class="adult_service",
        evidence_codes=("adult_service_subject_verified",),
        actor_user_id=9,
        permission_snapshot={"adult_content_attest": True, "permission_version": 4},
        expires_at=datetime.now() + timedelta(days=1),
        task_config_revision=revision,
        policy_version=1,
    )


def test_general_binding_does_not_require_adult_attestation() -> None:
    with Session(_engine()) as session:
        _seed(session)
        policy = _policy(session)

        binding = bind_task_policy(session, TaskBindingSpec(
            task_id="task-1",
            policy_version_id=policy.id,
            allowed_routes=("general",),
            attestation_ids=(),
            scope_refs=(),
            approved_by="approver",
        ))

        assert binding.allowed_routes == ["general"]
        assert_route_authorized(
            session,
            binding,
            route="general",
            scope_type="task_group",
            scope_id="group-7",
        )


def test_adult_binding_requires_strong_current_evidence_for_every_scope() -> None:
    with Session(_engine()) as session:
        _seed(session)
        policy = _policy(session)
        attestation = create_adult_attestation(session, _attestation_spec())
        binding = bind_task_policy(session, TaskBindingSpec(
            task_id="task-1",
            policy_version_id=policy.id,
            allowed_routes=("general", "adult_service_sensory"),
            attestation_ids=(attestation.id,),
            scope_refs=(("task_group", "group-7"),),
            approved_by="approver",
        ))

        assert_route_authorized(
            session,
            binding,
            route="adult_service_sensory",
            scope_type="task_group",
            scope_id="group-7",
        )
        with pytest.raises(AiContentPolicyConflict, match="adult_attestation_stale"):
            assert_route_authorized(
                session,
                binding,
                route="adult_service_sensory",
                scope_type="task_group",
                scope_id="group-8",
            )


def test_weak_word_and_revision_mismatch_cannot_authorize_adult_route() -> None:
    with Session(_engine()) as session:
        _seed(session)
        policy = _policy(session)
        weak = _attestation_spec()
        weak = AttestationSpec(
            **{**weak.__dict__, "evidence_codes": ("teacher_keyword_only",)}
        )
        with pytest.raises(ValueError, match="evidence_weak"):
            create_adult_attestation(session, weak)

        stale = create_adult_attestation(
            session,
            _attestation_spec(revision=2),
        )
        with pytest.raises(AiContentPolicyConflict, match="scope_mismatch"):
            bind_task_policy(session, TaskBindingSpec(
                task_id="task-1",
                policy_version_id=policy.id,
                allowed_routes=("adult_service_sensory",),
                attestation_ids=(stale.id,),
                scope_refs=(("task_group", "group-7"),),
                approved_by="approver",
            ))


def test_formal_task_activation_binds_active_policy_and_provider_routes() -> None:
    with Session(_engine()) as session:
        task = _seed(session)
        policy = _policy(session)
        task.type_config = {
            "target_group_id": 7,
            "ai_two_stage_enabled": True,
            "ai_content_route_v2_enabled": True,
            "ai_content_policy_version_id": policy.id,
            "ai_content_allowed_routes": ["general"],
            "ai_content_attestation_ids": [],
        }
        _seed_provider_routes(session)

        activate_task_ai_content_config(session, task)

        binding = session.query(TaskAiContentPolicyBinding).one()
        assert binding.policy_version_id == policy.id
        assert binding.allowed_routes == ["general"]


def test_route_v2_activation_rejects_missing_two_stage_without_binding() -> None:
    with Session(_engine()) as session:
        task = _seed(session)
        policy = _policy(session)
        task.type_config = {
            "target_group_id": 7,
            "ai_two_stage_enabled": False,
            "ai_content_route_v2_enabled": True,
            "ai_content_policy_version_id": policy.id,
            "ai_content_allowed_routes": ["general"],
            "ai_content_attestation_ids": [],
        }
        _seed_provider_routes(session)

        with pytest.raises(ValueError, match="ai_content_route_v2_requires_two_stage"):
            activate_task_ai_content_config(session, task)

        assert session.query(TaskAiContentPolicyBinding).count() == 0


def _seed_provider_routes(session: Session) -> None:
    session.add_all((
        AiProvider(
            id=1,
            provider_name="generator",
            base_url="mock://generator",
            model_name="generator-v1",
            api_key_ciphertext="cipher",
            credential_enabled=True,
            is_active=False,
        ),
        AiProvider(
            id=2,
            provider_name="reviewer",
            base_url="mock://reviewer",
            model_name="reviewer-v1",
            api_key_ciphertext="cipher",
            credential_enabled=True,
        ),
    ))
    session.flush()
    for index, purpose in enumerate((
        "group_context_route",
        "group_realize_general",
        "group_semantic_review",
    ), 1):
        route = TenantAiProviderRouteSet(
            tenant_id=1,
            purpose=purpose,
            revision=1,
            status="active",
            content_hash=str(index) * 64,
        )
        session.add(route)
        session.flush()
        session.add(TenantAiProviderRouteItem(
            route_set_id=route.id,
            priority=1,
            provider_id=2 if purpose == "group_semantic_review" else 1,
            model_name="reviewer-v1" if purpose == "group_semantic_review" else "generator-v1",
        ))
