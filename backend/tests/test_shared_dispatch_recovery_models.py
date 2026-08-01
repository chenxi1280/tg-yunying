from __future__ import annotations

from sqlalchemy import create_engine, inspect

import pytest

from app.database import Base


pytestmark = pytest.mark.no_postgres


def test_shared_dispatch_recovery_schema_contains_control_facts() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    inspector = inspect(engine)

    assert {
        "dispatch_runtime_shard_states",
        "ai_content_scope_takeover_batches",
        "ai_content_scope_takeover_items",
        "remote_reconcile_cases",
        "gateway_request_evidence_journals",
    } <= set(inspector.get_table_names())
    assert {
        "runtime_shard_total",
        "topology_fingerprint",
        "capacity_config_fingerprint",
        "contract_activation_state",
    } <= _columns(inspector, "dispatch_claim_scopes")
    assert "effective_unclaimed_count" in _columns(
        inspector,
        "dispatch_claim_windows",
    )
    assert "dispatch_contract_version" in _columns(
        inspector,
        "dispatch_claim_shard_allocations",
    )
    assert "remote_fact_id" in _columns(
        inspector,
        "remote_reconcile_cases",
    )
    assert "remote_fact_id" in _columns(
        inspector,
        "gateway_request_evidence_journals",
    )


def _columns(inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}
