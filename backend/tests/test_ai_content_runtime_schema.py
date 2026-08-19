from __future__ import annotations

import importlib

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AiContentPolicyVersion,
    AiContentWindowPlan,
    AiContentWindowPlanSlot,
    AiProvider,
    ContextScopeRevision,
    FulfillmentShortfallFact,
    GenerationJob,
    SourcePacingCapacityPlan,
    TenantAiProviderRouteItem,
    TenantAiProviderRouteSet,
)
from app.services.task_center.ai_generation_parallel import OPEN_GENERATION_JOB_PREDICATE


pytestmark = pytest.mark.no_postgres


def _engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def _provider(provider_id: int) -> AiProvider:
    return AiProvider(
        id=provider_id,
        provider_name=f"provider-{provider_id}",
        base_url="mock://provider",
        model_name=f"model-{provider_id}",
        api_key_ciphertext="ciphertext",
        credential_enabled=True,
        is_active=provider_id == 1,
    )


def test_v2_tables_and_generation_columns_are_registered() -> None:
    expected = {
        "ai_content_policy_versions",
        "adult_subject_attestations",
        "task_ai_content_policy_bindings",
        "context_scope_revisions",
        "ai_content_window_plans",
        "ai_content_window_plan_slots",
        "tenant_ai_provider_route_sets",
        "tenant_ai_provider_route_items",
        "ai_provider_attempts",
        "fulfillment_shortfall_facts",
        "source_pacing_capacity_policy_versions",
        "source_pacing_capacity_plans",
    }
    assert expected <= set(Base.metadata.tables)
    columns = set(GenerationJob.__table__.columns.keys())
    assert {
        "generation_stage",
        "provider_route_set_revision",
        "provider_route_snapshots",
        "window_slot_id",
    } <= columns
    assert OPEN_GENERATION_JOB_PREDICATE == "state IN ('pending','generating','unknown')"


def test_route_set_allows_ordered_multiple_enabled_credentials() -> None:
    with Session(_engine()) as session:
        session.add_all((_provider(1), _provider(2)))
        route_set = TenantAiProviderRouteSet(
            tenant_id=1,
            purpose="group_context_route",
            revision=1,
            status="active",
            content_hash="a" * 64,
        )
        session.add(route_set)
        session.flush()
        session.add_all((
            TenantAiProviderRouteItem(
                route_set_id=route_set.id,
                priority=1,
                provider_id=2,
                model_name="model-2",
            ),
            TenantAiProviderRouteItem(
                route_set_id=route_set.id,
                priority=2,
                provider_id=1,
                model_name="model-1",
            ),
        ))
        session.commit()

        items = session.query(TenantAiProviderRouteItem).order_by(
            TenantAiProviderRouteItem.priority
        ).all()
        assert [item.provider_id for item in items] == [2, 1]


def test_only_one_active_route_revision_per_purpose() -> None:
    with Session(_engine()) as session:
        session.add(TenantAiProviderRouteSet(
            tenant_id=1,
            purpose="group_semantic_review",
            revision=1,
            status="active",
            content_hash="a" * 64,
        ))
        session.commit()
        session.add(TenantAiProviderRouteSet(
            tenant_id=1,
            purpose="group_semantic_review",
            revision=2,
            status="active",
            content_hash="b" * 64,
        ))
        with pytest.raises(IntegrityError):
            session.commit()


def test_additive_migration_chain_keeps_single_active_compatibility() -> None:
    migration_155 = importlib.import_module(
        "migrations.versions.0155_ai_content_policy_routes"
    )
    migration_156 = importlib.import_module(
        "migrations.versions.0156_ai_content_runtime"
    )
    assert migration_155.down_revision == "0154_account_pacing_action_state"
    assert migration_156.down_revision == migration_155.revision
    assert "credential_enabled" in {
        column.name for column in AiProvider.__table__.columns
    }


def test_runtime_models_use_bounded_file_backed_contracts() -> None:
    assert AiContentPolicyVersion.__table__.c.policy_hash.type.length == 64
    assert AiContentWindowPlan.__table__.c.plan_hash.type.length == 64
    assert AiContentWindowPlanSlot.__table__.c.route_evidence_hash.type.length == 64
    assert ContextScopeRevision.__table__.c.context_scope_revision.nullable is False
    assert FulfillmentShortfallFact.__table__.c.owner_id.type.length == 255
    assert isinstance(SourcePacingCapacityPlan.__table__.c.capacity_slots.type, JSON)
