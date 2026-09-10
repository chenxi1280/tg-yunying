from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from tests.owner_postgres_support import owner_engine
from test_account_direct_egress_postgres import _seed
from app.services.task_center.direct_search_runtime import (
    build_direct_search_environment, resolve_direct_search_runtime,
)
from app.services.task_center.direct_rank_search import direct_rank_transport_fields
from app.services.task_center.payloads import SearchRankDeboostPayload
from app.services.task_center.rank_deboost_runtime_authorization import resolve_rank_deboost_runtime_authorization

pytestmark = pytest.mark.isolated_postgres
SETTINGS = SimpleNamespace(telegram_direct_egress_region="sv", telegram_direct_egress_ip="8.8.8.8")


def test_direct_context_uses_current_session_and_preserves_historical_proxy(owner_engine):
    with Session(owner_engine) as session:
        account, authorization = _seed(session)
        environment = build_direct_search_environment(session, account, settings=SETTINGS)
        runtime = resolve_direct_search_runtime(session, account, environment.runtime_environment, settings=SETTINGS)
        assert runtime.session_ciphertext == "canonical-session"
        assert runtime.credentials.proxy_id is None
        assert environment.client_metadata == {}
        assert "proxy_id" not in environment.runtime_environment
        assert account.proxy_id == authorization.proxy_id
        assert account.proxy_id is not None


def test_click_payload_and_dispatcher_use_owner_metadata_and_current_authorization(owner_engine, monkeypatch):
    from app.services.task_center.executors.search_join_group import PayloadInput, SearchJoinPlan, _payload
    from app.services.task_center.dispatcher import _search_join_runtime_authorization
    monkeypatch.setattr("app.services.task_center.dispatcher.get_settings", lambda: SETTINGS)
    with Session(owner_engine) as session:
        account, authorization = _seed(session)
        environment = build_direct_search_environment(session, account, settings=SETTINGS)
        config = {"search_execution_mode": "click_only", "keyword_hashes": ["a" * 64],
                  "keyword_text_ciphertexts": ["ciphertext"]}
        plan = SearchJoinPlan("jisou", "a" * 64, None, {}, "profile-v1", {})
        payload = _payload(PayloadInput(config, plan, "a" * 64, account, environment))
        assert payload.client_metadata == {}
        assert payload.authorization_id == authorization.id
        resolved = _search_join_runtime_authorization(session, account, payload)
        assert resolved.credentials.proxy_id is None
        assert resolved.session_ciphertext == "canonical-session"


@pytest.mark.parametrize("field", ["connection_generation", "authorization_generation"])
def test_generation_change_invalidates_frozen_search_plan(owner_engine, field):
    with Session(owner_engine) as session:
        account, _ = _seed(session)
        environment = build_direct_search_environment(session, account, settings=SETTINGS)
        setattr(account, field, getattr(account, field) + 1)
        with pytest.raises(ValueError, match="authorization_generation_stale"):
            resolve_direct_search_runtime(session, account, environment.runtime_environment, settings=SETTINGS)


def test_rank_direct_authorization_needs_no_group_proxy_binding(owner_engine, monkeypatch):
    from app.models import AccountPool
    monkeypatch.setattr("app.services.task_center.direct_rank_search.get_settings", lambda: SETTINGS)
    monkeypatch.setattr("app.services.task_center.rank_deboost_runtime_authorization.get_settings", lambda: SETTINGS)
    with Session(owner_engine) as session:
        account, _ = _seed(session)
        pool = AccountPool(tenant_id=account.tenant_id, name="rank", pool_purpose="rank_deboost")
        session.add(pool)
        session.flush()
        account.pool_id = pool.id
        payload = SearchRankDeboostPayload(
            bot_username="jisou", keyword_hash="a" * 64, account_pool_id=pool.id,
            **direct_rank_transport_fields(session, account),
        )
        resolved = resolve_rank_deboost_runtime_authorization(session, account, payload)
        assert resolved.session_ciphertext == "canonical-session"
        assert resolved.credentials.proxy_id is None
        assert payload.proxy_airport_node_id is None
        changed = payload.model_copy(update={"account_pool_id": pool.id + 1})
        with pytest.raises(ValueError, match="account_pool_mismatch"):
            resolve_rank_deboost_runtime_authorization(session, account, changed)
