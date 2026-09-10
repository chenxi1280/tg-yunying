from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
import requests

from app.integrations.telegram.contracts import DeveloperAppCredentials
from app.integrations.telegram.direct_search import DirectSearchExecution
from app.search_transport import DIRECT_SEARCH_TRANSPORT_VERSION, direct_proof_matches
from app.services.task_center.direct_search_results import normalize_direct_search_result
from app.services.task_center.channel_remote_evidence import remote_mutation_state


@pytest.fixture
def transport():
    settings = SimpleNamespace(telegram_owner_mode="server", telegram_direct_egress_region="sv",
                               telegram_direct_egress_ip="8.8.8.8")
    credentials = DeveloperAppCredentials(
        app_id=1, api_id=100, api_hash="test", credentials_version=1,
        tenant_id=1, account_id=10, authorization_id=20,
        authorization_generation=2, connection_generation=3, authorization_fact_version=4,
    )
    runtime = {
        "transport_contract_version": DIRECT_SEARCH_TRANSPORT_VERSION,
        "transport_mode": "direct", "client_metadata_policy": "owner_managed",
        "direct_egress_region": "sv", "direct_egress_ip": "8.8.8.8",
        "tenant_id": 1, "account_id": 10, "authorization_id": 20,
        "authorization_role": "primary", "authorization_fact_version": 4,
        "authorization_generation": 2, "connection_generation": 3,
        "developer_app_id": 1, "developer_app_api_id": 100, "developer_app_version": 1,
    }
    return settings, credentials, {"runtime_environment": runtime, "client_metadata": {}}


def execution(settings, observations, owner):
    def probe():
        observations.append("probe")
        return "8.8.8.8"
    return DirectSearchExecution(settings, probe=probe, owner_identity=lambda: owner)


def test_owner_observation_is_reused_without_fabricating_a_fresh_time(transport):
    settings, credentials, payload = transport
    observations = []
    run = execution(settings, observations, str(uuid4()))
    first = run.run(payload, credentials, lambda: {"success": True})
    second = run.run(payload, credentials, lambda: {"success": True})
    assert observations == ["probe"]
    assert first["transport_evidence"] == second["transport_evidence"]
    assert direct_proof_matches(payload["runtime_environment"], first["transport_evidence"])


@pytest.mark.parametrize("field,value", [("authorization_generation", 9), ("authorization_id", 99),
                                          ("connection_generation", 9), ("tenant_id", 2)])
def test_credential_fence_rejects_before_observation_and_business_call(transport, field, value):
    settings, credentials, payload = transport
    observations, calls = [], []
    run = execution(settings, observations, str(uuid4()))
    result = run.run(payload, replace(credentials, **{field: value}), lambda: calls.append(True))
    assert result["error_code"] == "direct_search_credential_fence_mismatch"
    assert result["remote_mutation_started"] is False
    assert observations == calls == []


@pytest.mark.parametrize("field,value", [("proxy_id", 0), ("proxy_binding_id", 12),
                                          ("proxy_egress_guard", "verified")])
def test_direct_declaration_does_not_accept_fake_proxy_fields(transport, field, value):
    settings, credentials, payload = transport
    changed = {**payload, "runtime_environment": {**payload["runtime_environment"], field: value}}
    calls = []
    result = execution(settings, calls, str(uuid4())).run(changed, credentials, lambda: calls.append(True))
    assert result["error_code"] == "direct_search_proxy_fields_forbidden"
    assert calls == []


def test_legacy_contract_and_local_gateway_are_rejected(transport):
    settings, credentials, payload = transport
    calls = []
    run = execution(settings, calls, str(uuid4()))
    assert run.run({"runtime_environment": {}}, credentials, lambda: None)["remote_mutation_started"] is False
    local = SimpleNamespace(**{**vars(settings), "telegram_owner_mode": "local"})
    result = execution(local, calls, "").run(payload, credentials, lambda: None)
    assert result["error_code"] == "direct_search_owner_required"
    assert calls == []


def test_observation_mismatch_does_not_invoke_telegram(transport):
    settings, credentials, payload = transport
    calls = []
    run = DirectSearchExecution(settings, probe=lambda: "1.1.1.1", owner_identity=lambda: str(uuid4()))
    result = run.run(payload, credentials, lambda: calls.append(True))
    assert result["error_code"] == "direct_search_egress_mismatch"
    assert result["remote_mutation_started"] is False and calls == []


def test_probe_error_is_explicit_and_never_cached_as_success(transport):
    settings, credentials, payload = transport
    def fail():
        raise requests.ConnectionError("probe unavailable")
    run = DirectSearchExecution(settings, probe=fail, owner_identity=lambda: str(uuid4()))
    result = run.run(payload, credentials, lambda: pytest.fail("unexpected Telegram call"))
    assert result["error_code"] == "direct_search_egress_probe_failed"
    assert result["error_type"] == "ConnectionError"


def test_missing_transport_proof_preserves_observed_click_and_forbids_replay(transport):
    _, _, payload = transport
    reported = {"success": True, "target_click_observed": True, "remote_mutation_started": False}
    result = normalize_direct_search_result(payload["runtime_environment"], reported)
    assert reported["success"] is True
    assert result["target_click_observed"] is True
    assert result["remote_mutation_started"] is False
    assert result["success"] is False and result["gateway_outcome_unknown"] is True
    action = SimpleNamespace(result=result)
    assert remote_mutation_state(action, SimpleNamespace(result_snapshot=reported)) == "unknown"


def test_confirmed_pre_gateway_failure_does_not_require_a_successful_proof(transport):
    _, _, payload = transport
    result = normalize_direct_search_result(payload["runtime_environment"], {
        "success": False, "remote_mutation_started": False, "error_code": "direct_search_egress_mismatch",
    })
    assert "gateway_outcome_unknown" not in result


def test_empty_proxy_resource_does_not_merge_distinct_direct_accounts():
    from app.services.task_center.search_click_solver_snapshot import _union_shared_resources
    from app.services.task_center.search_click_assignment_solver import SearchClickCandidatePath

    paths = tuple(SearchClickCandidatePath(
        key=str(account), account_id=account, authorization_id=account + 10, keyword_hash="a" * 64,
        proxy_route_id="", protocol_sample_version="v1", hard_safe_remaining_capacity=1,
        confirmed_click_count_today=0, last_click_opportunity_at=None, persistent_account_cursor=0,
    ) for account in (1, 2))
    parent = {"p:1": "p:1", "p:2": "p:2"}
    _union_shared_resources(parent, paths)
    assert parent == {"p:1": "p:1", "p:2": "p:2"}
