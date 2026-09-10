from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from tests.owner_postgres_support import owner_engine
from test_account_direct_egress_postgres import _seed
from app.models import ExecutionCircuitState, ExecutionResiliencePolicyRevision
from app.services._common import _now
from app.services.task_center.ai_group_circuit_eligibility import blocked_coverage_account_ids
from app.services.task_center.engagement_runtime_circuit import circuit_blocker, _failure_domains
from app.services.task_center.engagement_runtime_domains import proxy_capacity_blocker
from app.services.task_center.engagement_runtime_resources import _new_remote_fence

pytestmark = pytest.mark.isolated_postgres


def test_direct_account_circuit_isolation_and_proxy_capacity_not_applicable(owner_engine):
    with Session(owner_engine) as session:
        account, _ = _seed(session)
        policy = ExecutionResiliencePolicyRevision(tenant_id=account.tenant_id)
        session.add(policy)
        session.flush()
        for kind, key in (("account", f"account:{account.id}"), ("proxy_route", f"proxy:{account.proxy_id}"),
                          ("proxy_egress", "exit:8.8.8.8")):
            session.add(ExecutionCircuitState(tenant_id=account.tenant_id, resilience_policy_revision_id=policy.id,
                                             domain_kind=kind, domain_key=key, state="open",
                                             opened_until=_now() + timedelta(minutes=1)))
        session.flush()
        scope = {"tenant_id": account.tenant_id, "route_key": "", "egress_key": ""}
        assert circuit_blocker(session, account_id=account.id, **scope)[0] == "execution_circuit_open"
        assert circuit_blocker(session, account_id=account.id + 1, **scope) is None
        assert proxy_capacity_blocker(session, policy=policy, **scope) is None
        task = SimpleNamespace(tenant_id=account.tenant_id, type="group_ai_chat",
                               fulfillment_contract_version="fact_first_v3",
                               type_config={"engagement_contract_version": "unified_engagement_v1"})
        assert blocked_coverage_account_ids(session, task) == {account.id}
        lease = SimpleNamespace(account_id=account.id, proxy_route_key="", proxy_egress_key="")
        assert _failure_domains(lease, "network timeout", None) == (("account", f"account:{account.id}"),)


def test_fence_records_actual_direct_transport_separately_from_historical_proxy(owner_engine):
    with Session(owner_engine) as session:
        account, _ = _seed(session)
        action = SimpleNamespace(id="action", tenant_id=account.tenant_id)
        fence = _new_remote_fence(action, attempt=SimpleNamespace(id="attempt"),
                                  account=account, policy=SimpleNamespace(id="policy"))
        assert fence.domain_keys["transport_mode"] == "direct"
        assert fence.domain_keys["proxy_id"] is None
        assert fence.domain_keys["historical_proxy_id"] == account.proxy_id
        assert fence.domain_keys["circuit_scope"] == "account"
