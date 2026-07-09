"""search_rank_deboost 风控告警生成测试（Task 19）。

覆盖 spec 6 类告警 scenario：
- rank_deboost_join_button_violation: Executor 误点加入按钮
- rank_deboost_group_ip_drift: 分组级共享出口 IP 漂移
- rank_deboost_node_unreachable: 分组级绑定节点不可达
- rank_deboost_account_isolation_violation: 账号组隔离违规
- rank_deboost_exempt_group_missing: 任务启动时豁免群未预选
- rank_deboost_all_exempt_clicks: 所有结果都被白名单豁免
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountPool,
    AccountStatus,
    Action,
    BotProtocolSample,
    ProxyAirportNode,
    ProxyAirportSubscription,
    SearchRankDeboostAlert,
    Task,
    Tenant,
    TgAccount,
)
from app.models.search_rank_deboost import (
    AccountGroupProxyBinding,
    SearchRankDeboostActionStat,
    SearchRankDeboostExemptGroup,
)
from app.services._common import _now
from app.services.task_center.dispatcher import dispatch_action
from app.services.task_center.executors.search_rank_deboost import (
    build_plan,
    execute_search_rank_deboost,
)
from app.services.task_center.payloads import SearchRankDeboostPayload


pytestmark = pytest.mark.no_postgres


KEYWORD_HASH_A = "a" * 64


def _build_engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def _seed_protocol_samples(session: Session, tenant_id: int = 1, bot_username: str = "jisou") -> None:
    for _ in range(2):
        session.add(BotProtocolSample(tenant_id=tenant_id, bot_username=bot_username, sample_type="start_response", sample_purpose="rank_deboost", is_active=True))
    for _ in range(5):
        session.add(BotProtocolSample(tenant_id=tenant_id, bot_username=bot_username, sample_type="search_results", sample_purpose="rank_deboost", is_active=True))
    for _ in range(3):
        session.add(BotProtocolSample(tenant_id=tenant_id, bot_username=bot_username, sample_type="pagination_response", sample_purpose="rank_deboost", is_active=True))
    for effect in ("navigate_only", "join_candidate", "external_http_url"):
        session.add(BotProtocolSample(
            tenant_id=tenant_id,
            bot_username=bot_username,
            sample_type="button_structure",
            sample_purpose="rank_deboost",
            is_active=True,
            structure_json={"button_effect": effect},
        ))
    for _ in range(3):
        session.add(BotProtocolSample(tenant_id=tenant_id, bot_username=bot_username, sample_type="exit_ip_observation", sample_purpose="rank_deboost", is_active=True))


def _make_task(
    session: Session,
    *,
    tenant_id: int = 1,
    account_pool_id: int = 10,
    proxy_airport_node_id: int = 20,
    target_group_ids: list[int] | None = None,
    keywords: list[dict] | None = None,
    config: dict | None = None,
) -> Task:
    type_config: dict = {
        "search_bots": ["jisou"],
        "keywords": keywords or [{"text": "关键词A"}],
        "target_group_ids": target_group_ids or [1001],
        "account_pool_id": account_pool_id,
        "proxy_airport_node_id": proxy_airport_node_id,
        "notes": "",
    }
    if config:
        type_config.update(config)
    task = Task(
        id=str(uuid4()),
        tenant_id=tenant_id,
        name="降权任务",
        type="search_rank_deboost",
        status="running",
        priority=3,
        timezone="Asia/Shanghai",
        account_config={},
        pacing_config={},
        failure_policy={},
        type_config=type_config,
        stats={},
        next_run_at=_now(),
    )
    session.add(task)
    return task


def _seed_base(
    session: Session,
    *,
    account_pool_id: int = 10,
    proxy_node_id: int = 20,
    observed_exit_ip: str = "1.1.1.1",
    account_ids: list[int] | None = None,
    exempt_username: str = "exempt_group",
) -> tuple[AccountGroupProxyBinding, list[TgAccount]]:
    session.add(Tenant(id=1, name="默认运营空间"))
    session.add(ProxyAirportSubscription(id=1, tenant_id=1, name="主订阅", enabled=True, sync_status="synced", healthy_node_count=3))
    session.add(AccountPool(id=account_pool_id, tenant_id=1, name="降权分组", pool_purpose="rank_deboost"))
    session.add(ProxyAirportNode(id=proxy_node_id, tenant_id=1, subscription_id=1, node_key=f"node-{proxy_node_id}", status="healthy", observed_exit_ip=observed_exit_ip))
    _seed_protocol_samples(session)

    binding = AccountGroupProxyBinding(
        id=1,
        tenant_id=1,
        account_pool_id=account_pool_id,
        proxy_airport_node_id=proxy_node_id,
        binding_scope="group",
        observed_exit_ip=observed_exit_ip,
        status="active",
        bound_by="tester",
    )
    session.add(binding)

    session.add(SearchRankDeboostExemptGroup(
        id=str(uuid4()),
        tenant_id=1,
        task_id="placeholder",
        exempt_group_username=exempt_username,
        exempt_group_peer_id="-100999",
        exempt_group_title="豁免群",
        exempt_group_match_strategy="username",
        selected_at=_now(),
        selected_by="tester",
    ))

    accounts: list[TgAccount] = []
    resolved_account_ids = account_ids if account_ids is not None else [100]
    for account_id in resolved_account_ids:
        account = TgAccount(
            id=account_id,
            tenant_id=1,
            pool_id=account_pool_id,
            display_name=f"降权账号{account_id}",
            phone_masked=str(account_id),
            status=AccountStatus.ACTIVE.value,
            account_identity="rank_deboost",
            health_score=95,
        )
        session.add(account)
        accounts.append(account)

    session.flush()
    return binding, accounts


def _make_payload(
    task: Task,
    *,
    account_id: int = 100,
    binding_id: int = 1,
    keyword_hash: str = KEYWORD_HASH_A,
    keyword_text: str = "关键词A",
    target_group_ids: list[int] | None = None,
    exempt_group_username: str = "exempt_group",
    account_pool_id: int = 10,
    proxy_node_id: int = 20,
    observed_exit_ip: str = "1.1.1.1",
) -> SearchRankDeboostPayload:
    return SearchRankDeboostPayload(
        bot_username="jisou",
        keyword_hash=keyword_hash,
        keyword_text_ciphertext=keyword_text,
        target_group_ids=target_group_ids or [1001],
        account_pool_id=account_pool_id,
        proxy_airport_node_id=proxy_node_id,
        exempt_group_username=exempt_group_username,
        dwell_seconds_min=10,
        dwell_seconds_max=30,
        runtime_environment={
            "proxy_egress_guard": "verified",
            "group_proxy_binding_id": str(binding_id),
            "proxy_airport_node_id": str(proxy_node_id),
            "account_pool_id": str(account_pool_id),
            "observed_exit_ip": observed_exit_ip,
        },
    )


def _make_action(session: Session, task: Task, account: TgAccount, payload: SearchRankDeboostPayload) -> Action:
    action = Action(
        id=str(uuid4()),
        tenant_id=task.tenant_id,
        task_id=task.id,
        task_type=task.type,
        action_type="search_rank_deboost",
        account_id=account.id,
        scheduled_at=_now(),
        status="executing",
        payload=payload.model_dump(mode="json"),
        result={},
    )
    session.add(action)
    session.flush()
    return action


def _make_search_results(
    count: int = 5,
    *,
    target_position: int = 3,
    target_username: str = "my_target",
    exempt_position: int | None = 5,
    exempt_username: str = "exempt_group",
    include_buttons: bool = True,
    button_effect: str = "navigate_only",
) -> list[dict]:
    items: list[dict] = []
    for position in range(1, count + 1):
        if position == target_position:
            username = target_username
        elif exempt_position is not None and position == exempt_position:
            username = exempt_username
        else:
            username = f"competitor_{position}"
        item: dict = {
            "position": position,
            "username": username,
            "peer_id": f"-100{position}",
            "title": f"群 {position}",
        }
        if include_buttons:
            item["buttons"] = [
                {"text": "详情", "url": "https://example.com", "effect": button_effect, "position": position},
            ]
        items.append(item)
    items[target_position - 1]["id"] = 1001
    return items


# ==================== SubTask 19.2: join_button_violation 告警 ====================


def test_join_button_violation_generates_alert() -> None:
    """Executor 误点加入按钮时生成 rank_deboost_join_button_violation 告警。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        session.commit()

        search_results = _make_search_results(count=5, target_position=3, button_effect="join_candidate")
        gateway_execute = lambda account_id, payload_data, keyword_text: {"success": True, "search_results": search_results}

        from app.services.task_center.executors import search_rank_deboost as executor_module

        original_navigable = executor_module.NAVIGABLE_BUTTON_EFFECTS
        executor_module.NAVIGABLE_BUTTON_EFFECTS = {"navigate_only", "join_candidate"}
        try:
            result = execute_search_rank_deboost(session, action, account, payload, gateway_execute=gateway_execute, probe_exit_ip="1.1.1.1")
        finally:
            executor_module.NAVIGABLE_BUTTON_EFFECTS = original_navigable

        assert result["success"] is False
        assert result["join_button_violation"] is True

        alerts = session.query(SearchRankDeboostAlert).filter_by(
            alert_type="rank_deboost_join_button_violation"
        ).all()
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.severity == "critical"
        assert alert.task_id == task.id
        assert alert.action_id == action.id
        assert alert.account_id == account.id
        assert alert.status == "alerting"
        assert alert.reason_code == "join_button_violation"
        assert alert.context.get("button_effect") == "join_candidate"


# ==================== SubTask 19.3: 代理出口 IP 漂移 / 节点不可达 告警 ====================


def test_proxy_egress_ip_drift_generates_alert_in_executor() -> None:
    """Executor 检测到出口 IP 漂移时生成 rank_deboost_group_ip_drift 告警。"""
    engine = _build_engine()
    with Session(engine) as session:
        # binding observed_exit_ip="1.1.1.1"，但 probe_exit_ip 传不同值
        binding, accounts = _seed_base(session, observed_exit_ip="1.1.1.1")
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        session.commit()

        result = execute_search_rank_deboost(session, action, account, payload, probe_exit_ip="9.9.9.9")

        assert result["success"] is False
        assert result["skip_reason"] == "proxy_egress_guard_failed"

        alerts = session.query(SearchRankDeboostAlert).filter_by(
            alert_type="rank_deboost_group_ip_drift"
        ).all()
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.severity == "critical"
        assert alert.context.get("observed_exit_ip") == "1.1.1.1"
        assert alert.context.get("probe_exit_ip") == "9.9.9.9"
        assert alert.context.get("group_proxy_binding_id") == binding.id


def test_proxy_egress_node_unreachable_generates_alert_when_probe_empty() -> None:
    """probe_exit_ip 为空（节点不可达）时生成 rank_deboost_node_unreachable 告警。"""
    engine = _build_engine()
    with Session(engine) as session:
        # binding observed_exit_ip 为空，模拟首次探测也未通
        binding, accounts = _seed_base(session, observed_exit_ip="")
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        session.commit()

        result = execute_search_rank_deboost(session, action, account, payload, probe_exit_ip=None)

        assert result["success"] is False
        assert result["skip_reason"] == "proxy_egress_guard_failed"

        alerts = session.query(SearchRankDeboostAlert).filter_by(
            alert_type="rank_deboost_node_unreachable"
        ).all()
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.severity == "critical"
        assert alert.reason_code == "group_node_unreachable"


def test_proxy_egress_node_unreachable_generates_alert_when_binding_inactive() -> None:
    """binding 状态非 active 时生成 rank_deboost_node_unreachable 告警。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session, observed_exit_ip="1.1.1.1")
        # 把 binding 标记为 unbound
        binding.status = "unbound"
        binding.unbound_at = _now()
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        session.commit()

        result = execute_search_rank_deboost(session, action, account, payload, probe_exit_ip="1.1.1.1")

        assert result["success"] is False
        assert result["skip_reason"] == "proxy_egress_guard_failed"

        alerts = session.query(SearchRankDeboostAlert).filter_by(
            alert_type="rank_deboost_node_unreachable"
        ).all()
        assert len(alerts) == 1
        assert alerts[0].context.get("binding_active") is False


def test_dispatch_proxy_egress_drift_generates_alert(monkeypatch) -> None:
    """dispatch_action 在 gateway 本次出口探测为空时生成 node_unreachable 告警。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session, observed_exit_ip="1.1.1.1")
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        session.commit()

        def fake_gateway(_account_id, _payload_data, _keyword_text):
            return {"success": True, "search_results": []}

        from app.services import _common

        monkeypatch.setattr(_common.gateway, "execute_search_rank_deboost", fake_gateway, raising=False)

        result = dispatch_action(session, action)

        assert result is True
        assert action.status == "skipped"
        alerts = session.query(SearchRankDeboostAlert).filter_by(
            alert_type="rank_deboost_node_unreachable"
        ).all()
        assert len(alerts) == 1


# ==================== SubTask 19.4: 账号组隔离违规告警 ====================


def test_account_isolation_violation_alert_when_rank_deboost_account_used_by_other_task() -> None:
    """降权专用账号被其他任务（非 search_rank_deboost）选用时生成隔离违规告警。"""
    from app.models import SchedulingSetting

    engine = _build_engine()
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(AccountPool(id=10, tenant_id=1, name="降权分组", pool_purpose="rank_deboost"))
        # 降权专用账号
        account = TgAccount(
            id=100,
            tenant_id=1,
            pool_id=10,
            display_name="降权账号",
            phone_masked="100",
            status=AccountStatus.ACTIVE.value,
            account_identity="rank_deboost",
            health_score=95,
        )
        session.add(account)
        # 非 search_rank_deboost 任务（如 group_ai_chat）的 action
        task = Task(
            id=str(uuid4()),
            tenant_id=1,
            name="普通群聊任务",
            type="group_ai_chat",
            status="running",
            priority=3,
            timezone="Asia/Shanghai",
            account_config={},
            pacing_config={},
            failure_policy={},
            type_config={},
            stats={},
            next_run_at=_now(),
        )
        session.add(task)
        action = Action(
            id=str(uuid4()),
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="group_ai_chat",
            account_id=account.id,
            scheduled_at=_now(),
            status="executing",
            payload={},
            result={},
        )
        session.add(action)
        session.commit()

        from app.services.task_center.dispatcher import _apply_claim_account_policy

        allowed = _apply_claim_account_policy(session, action)

        assert allowed is False
        alerts = session.query(SearchRankDeboostAlert).filter_by(
            alert_type="rank_deboost_account_isolation_violation"
        ).all()
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.account_id == account.id
        assert alert.context.get("violation") == "rank_deboost_account_used_by_other"
        assert alert.severity == "warning"


def test_account_isolation_violation_alert_when_deboost_task_uses_normal_account() -> None:
    """降权任务选用普通账号时生成隔离违规告警。"""
    engine = _build_engine()
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(AccountPool(id=11, tenant_id=1, name="普通分组", pool_purpose="normal", is_default=True))
        # 普通账号
        account = TgAccount(
            id=200,
            tenant_id=1,
            pool_id=11,
            display_name="普通账号",
            phone_masked="200",
            status=AccountStatus.ACTIVE.value,
            account_identity="normal",
            health_score=90,
        )
        session.add(account)
        task = Task(
            id=str(uuid4()),
            tenant_id=1,
            name="降权任务",
            type="search_rank_deboost",
            status="running",
            priority=3,
            timezone="Asia/Shanghai",
            account_config={},
            pacing_config={},
            failure_policy={},
            type_config={},
            stats={},
            next_run_at=_now(),
        )
        session.add(task)
        action = Action(
            id=str(uuid4()),
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="search_rank_deboost",
            account_id=account.id,
            scheduled_at=_now(),
            status="executing",
            payload={},
            result={},
        )
        session.add(action)
        session.commit()

        from app.services.task_center.dispatcher import _apply_claim_account_policy

        allowed = _apply_claim_account_policy(session, action)

        assert allowed is False
        alerts = session.query(SearchRankDeboostAlert).filter_by(
            alert_type="rank_deboost_account_isolation_violation"
        ).all()
        assert len(alerts) == 1
        assert alerts[0].context.get("violation") == "deboost_task_used_normal_account"


# ==================== SubTask 19.4: 豁免群缺失告警 ====================


def test_exempt_group_missing_alert_when_no_exempt_group_record() -> None:
    """任务启动时豁免群记录不存在（使用占位 username）时生成告警。"""
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session)
        task = _make_task(session)
        # 删除 exempt group 记录，触发占位
        session.query(SearchRankDeboostExemptGroup).delete()
        session.commit()

        build_plan(session, task)

        alerts = session.query(SearchRankDeboostAlert).filter_by(
            alert_type="rank_deboost_exempt_group_missing"
        ).all()
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.task_id == task.id
        assert alert.severity == "warning"
        assert alert.reason_code == "exempt_group_pending_real_search"


def test_exempt_group_missing_alert_when_placeholder_username() -> None:
    """豁免群记录存在但 username=pending_real_search 时生成告警。"""
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session, exempt_username="pending_real_search")
        task = _make_task(session)
        session.query(SearchRankDeboostExemptGroup).update({SearchRankDeboostExemptGroup.task_id: task.id})
        session.commit()

        build_plan(session, task)

        alerts = session.query(SearchRankDeboostAlert).filter_by(
            alert_type="rank_deboost_exempt_group_missing"
        ).all()
        assert len(alerts) == 1


def test_no_exempt_group_missing_alert_when_real_exempt_group_present() -> None:
    """豁免群已预选真实 username 时不生成告警。"""
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session, exempt_username="real_exempt_group")
        task = _make_task(session)
        session.query(SearchRankDeboostExemptGroup).update({SearchRankDeboostExemptGroup.task_id: task.id})
        session.commit()

        build_plan(session, task)

        alerts = session.query(SearchRankDeboostAlert).filter_by(
            alert_type="rank_deboost_exempt_group_missing"
        ).all()
        assert len(alerts) == 0


# ==================== SubTask 19.4: all_exempt_clicks 告警 ====================


def test_all_exempt_clicks_generates_alert() -> None:
    """所有结果都被白名单豁免时生成 rank_deboost_all_exempt_clicks 告警。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        session.commit()

        # 只有一个搜索结果，且就是目标群 → all_exempt_clicks
        search_results = [{"position": 1, "username": "my_target", "peer_id": "-1001", "id": 1001, "title": "目标群", "buttons": []}]
        gateway_execute = lambda account_id, payload_data, keyword_text: {"success": True, "search_results": search_results}

        result = execute_search_rank_deboost(session, action, account, payload, gateway_execute=gateway_execute, probe_exit_ip="1.1.1.1")

        assert result["success"] is False
        assert result["skip_reason"] == "all_exempt_clicks"

        alerts = session.query(SearchRankDeboostAlert).filter_by(
            alert_type="rank_deboost_all_exempt_clicks"
        ).all()
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.severity == "info"
        assert alert.task_id == task.id
        assert alert.action_id == action.id


# ==================== 告警类别定义回归 ====================


def test_six_alert_types_defined() -> None:
    """回归：6 类告警类别常量已定义。"""
    from app.models.search_rank_deboost_alert import RANK_DEBOOST_ALERT_TYPES

    expected = {
        "rank_deboost_group_ip_drift",
        "rank_deboost_node_unreachable",
        "rank_deboost_join_button_violation",
        "rank_deboost_account_isolation_violation",
        "rank_deboost_exempt_group_missing",
        "rank_deboost_all_exempt_clicks",
    }
    assert RANK_DEBOOST_ALERT_TYPES == expected
