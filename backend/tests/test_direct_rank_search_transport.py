from types import SimpleNamespace

import pytest

from test_direct_search_transport import transport
from app.schemas.task_center import SearchRankDeboostTaskCreate
from app.services.task_center.payloads import SearchRankDeboostPayload
from app.services.task_center.executors import search_rank_deboost_runtime as runtime

pytestmark = pytest.mark.no_postgres


def test_new_rank_create_is_direct_and_rejects_proxy_selection():
    created = SearchRankDeboostTaskCreate(name="rank")
    assert created.transport_contract_version == "sv_current_direct_v1"
    with pytest.raises(ValueError, match="proxy_fields_forbidden"):
        SearchRankDeboostTaskCreate(name="rank", proxy_airport_node_id=1)
    with pytest.raises(ValueError, match="transport_contract_invalid"):
        SearchRankDeboostTaskCreate(name="rank", config={"transport_contract_version": "legacy_proxy_v1"})


def test_rank_payload_null_proxy_is_only_valid_for_direct_contract(transport):
    _, _, data = transport
    fields = {"bot_username": "jisou", "keyword_hash": "a" * 64, "account_pool_id": 1}
    payload = SearchRankDeboostPayload(**fields, runtime_environment=data["runtime_environment"])
    assert payload.proxy_airport_node_id is None
    with pytest.raises(ValueError, match="legacy_search_proxy_node_required"):
        SearchRankDeboostPayload(**fields)
    with pytest.raises(ValueError, match="proxy_fields_forbidden"):
        SearchRankDeboostPayload(**fields, runtime_environment=data["runtime_environment"], proxy_airport_node_id=1)


def test_rank_missing_proof_preserves_click_and_reservation_as_unknown(transport, monkeypatch):
    _, _, data = transport
    payload = SearchRankDeboostPayload(bot_username="jisou", keyword_hash="a" * 64,
                                     account_pool_id=1, runtime_environment=data["runtime_environment"])
    monkeypatch.setattr(runtime, "resolve_rank_deboost_runtime_authorization", lambda *_: object())
    reported = {"success": False, "execution_status": "confirmed", "remote_mutation_started": False,
                "click_outcomes": [{"status": "confirmed", "competitor_username": "observed"}]}
    monkeypatch.setattr(runtime, "_invoke_gateway", lambda *args, **kwargs: (True, reported))
    unknown = []
    monkeypatch.setattr(runtime, "mark_reservation_unknown", lambda session, action_id: unknown.append(action_id))
    monkeypatch.setattr(runtime, "release_reserved_reservation", lambda *_: pytest.fail("must not replay"))
    result = runtime.execute_search_rank_deboost(None, SimpleNamespace(id="action"), None, payload)
    assert result["execution_status"] == "unknown_after_click"
    assert result["click_outcomes"] == reported["click_outcomes"]
    assert result["remote_mutation_started"] is False
    assert unknown == ["action"]
