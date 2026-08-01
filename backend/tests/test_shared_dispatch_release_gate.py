from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres
ROOT = Path(__file__).resolve().parents[2]


def test_server_compose_uses_one_26_slot_two_shard_contract() -> None:
    compose = (ROOT / "docker-compose.server.yml").read_text()
    assert "DISPATCHER_SCOPE_CAPACITY: ${DISPATCHER_SCOPE_CAPACITY:-26}" in compose
    assert "DISPATCH_RUNTIME_SHARD_TOTAL: ${DISPATCH_RUNTIME_SHARD_TOTAL:-2}" in compose
    assert "DB_POOL_CONTROL_RESERVE: ${DB_POOL_CONTROL_RESERVE:-2}" in compose
    assert compose.count('ACCOUNT_SHARD_TOTAL: "2"') == 2
    assert 'ACCOUNT_SHARD_INDEX: "0"' in compose
    assert 'ACCOUNT_SHARD_INDEX: "1"' in compose


def test_release_runs_preparing_readiness_takeover_then_activation() -> None:
    script = (ROOT / "deploy" / "compose-up.sh").read_text()
    ordered_markers = (
        "manage_shared_dispatch_contract stage",
        "Starting new workers in fenced readiness",
        "manage_shared_dispatch_contract verify-ready",
        "manage_shared_dispatch_contract reconcile-ledger",
        "takeover_ai_content_scope preview",
        "takeover_ai_content_scope apply",
        "manage_shared_dispatch_contract activate",
        "manage_shared_dispatch_contract verify-active",
    )
    positions = [script.index(marker) for marker in ordered_markers]
    assert positions == sorted(positions)
    assert "--takeover-head-batch-id" in script


def test_post_deploy_requires_active_contract_verification() -> None:
    workflow = (ROOT / ".github" / "workflows" / "deploy-production.yml").read_text()
    assert "Verify active shared dispatch contract" in workflow
    assert "manage_shared_dispatch_contract verify-active" in workflow


def test_runtime_env_rejects_old_52_capacity_contract() -> None:
    script = (ROOT / "deploy" / "docker-env.sh").read_text()
    assert 'DISPATCHER_SCOPE_CAPACITY="${DISPATCHER_SCOPE_CAPACITY:-26}"' in script
    assert 'if [[ "$DISPATCHER_SCOPE_CAPACITY" != "26" ]]' in script
    assert 'if [[ "$DISPATCH_RUNTIME_SHARD_TOTAL" != "2" ]]' in script
    assert "ENABLE_EMBEDDED_WORKER must be false in production" in script


def test_production_env_template_matches_shared_dispatch_contract() -> None:
    template = (ROOT / ".env.production.example").read_text()
    assert "DISPATCHER_CONCURRENCY=20" in template
    assert "DISPATCHER_SCOPE_CAPACITY=26" in template
    assert "DISPATCH_RUNTIME_SHARD_TOTAL=2" in template
    assert "DB_POOL_CONTROL_RESERVE=2" in template
    assert "DISPATCH_SHARD_STALE_SECONDS=120" in template
    assert "DISPATCH_TOPOLOGY_FINGERPRINT_SCHEMA_VERSION=dispatch_topology_v1" in template
    assert "DISPATCH_REBUILD_CONTRACT_VERSION=dispatch-rebuild-v3" in template
