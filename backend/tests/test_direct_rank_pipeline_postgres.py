from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.owner_postgres_support import owner_engine
from test_account_direct_egress_postgres import _seed
from test_search_rank_deboost_executor import _seed_protocol_samples, _gateway_result
from app.models import AccountPool, Action, OperationTarget, SearchRankDeboostExemptGroup, Task
from app.models.search_rank_deboost import SearchRankDeboostActionStat, SearchRankDeboostClickReservation
from app.integrations.telegram.direct_search import DirectSearchExecution
from app.services.task_center.executors.search_rank_deboost_planner import build_plan
from app.services.task_center.executors.search_rank_deboost_runtime import execute_search_rank_deboost
from app.services.task_center.payloads import SearchRankDeboostPayload

pytestmark = pytest.mark.isolated_postgres
SETTINGS = SimpleNamespace(telegram_owner_mode="server", telegram_direct_egress_region="sv",
                           telegram_direct_egress_ip="8.8.8.8")


def seed_rank(session):
    account, _ = _seed(session)
    pool = AccountPool(tenant_id=account.tenant_id, name="rank", pool_purpose="rank_deboost")
    session.add(pool)
    session.flush()
    account.pool_id, account.account_identity = pool.id, "rank_deboost"
    target = OperationTarget(tenant_id=account.tenant_id, target_type="group", tg_peer_id="-1001",
                             title="target", username="my_target")
    session.add(target)
    session.flush()
    task = Task(tenant_id=account.tenant_id, name="direct rank", type="search_rank_deboost", status="running",
                account_config={"selection_mode": "group", "account_group_id": pool.id},
                type_config={"transport_contract_version": "sv_current_direct_v1", "search_bots": ["jisou"],
                             "keywords": [{"text": "test"}], "target_group_ids": [target.id],
                             "target_reference_type": "operation_target", "target_count": 10})
    session.add(task)
    session.flush()
    session.add(SearchRankDeboostExemptGroup(tenant_id=account.tenant_id, task_id=task.id,
                                            exempt_group_username="exempt_group", selected_by="tester"))
    _seed_protocol_samples(session, tenant_id=account.tenant_id)
    session.commit()
    return task, account


@pytest.mark.parametrize("status,expected", [("confirmed", "consumed"), ("unknown_after_click", "unknown"),
                                            ("target_not_in_results", "released")])
def test_direct_rank_planner_through_typed_result_without_proxy(owner_engine, monkeypatch, status, expected):
    monkeypatch.setattr("app.services.task_center.direct_rank_search.get_settings", lambda: SETTINGS)
    monkeypatch.setattr("app.services.task_center.rank_deboost_runtime_authorization.get_settings", lambda: SETTINGS)
    with Session(owner_engine) as session:
        task, account = seed_rank(session)
        assert build_plan(session, task) == 1
        action = session.scalar(select(Action).where(Action.task_id == task.id))
        payload = SearchRankDeboostPayload.model_validate(action.payload)
        assert payload.proxy_airport_node_id is None
        assert payload.runtime_environment["authorization_id"] == account.current_authorization_id
        execution = DirectSearchExecution(SETTINGS, probe=lambda: "8.8.8.8", owner_identity=lambda: str(uuid4()))

        def gateway(account_id, values, *, credentials, **kwargs):
            assert account_id == account.id and credentials.proxy_id is None
            return execution.run(values, credentials, lambda: _gateway_result(status))

        result = execute_search_rank_deboost(session, action, account, payload, gateway_execute=gateway)
        reservation = session.scalar(select(SearchRankDeboostClickReservation).where(
            SearchRankDeboostClickReservation.action_id == action.id))
        assert reservation.status == expected
        assert result["transport_evidence"]["observed_ip"] == "8.8.8.8"
        stat = session.scalar(select(SearchRankDeboostActionStat).where(SearchRankDeboostActionStat.action_id == action.id))
        assert bool(stat) == (status == "confirmed")
        if stat:
            assert stat.proxy_airport_node_id is None and stat.observed_exit_ip == "8.8.8.8"
