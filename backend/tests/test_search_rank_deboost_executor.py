"""search_rank_deboost Executor + Dispatcher + Pacing + Stats 集成测试（Task 13 + 14）。

覆盖 spec 场景：
- Planner build_plan 创建 action
- Executor: navigate_only_no_join_click、no_navigable_button、proxy_egress_guard_failed、
  target_not_in_results、all_exempt_clicks
- Pacing: 单账号每日上限、单关键词每日上限、分组 IP 每日上限、任务每小时上限、账号冷却
- join_button_violation 自检
- stats 记录写入正确
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountPool,
    AccountProxy,
    AccountStatus,
    Action,
    BotProtocolSample,
    ProxyAirportNode,
    ProxyAirportSubscription,
    Task,
    Tenant,
    TgAccount,
)
from app.models.search_rank_deboost import (
    AccountGroupProxyBinding,
    SearchRankDeboostActionStat,
    SearchRankDeboostClickReservation,
    SearchRankDeboostExemptGroup,
)
from app.services._common import _now
from app.services.task_center.dispatcher import dispatch_action
from app.services.task_center.executors.search_rank_deboost import (
    build_plan,
    execute_search_rank_deboost,
)
from app.services.task_center.payloads import SearchRankDeboostPayload
from app.services.task_center.search_rank_deboost_pacing import (
    DEFAULT_MAX_ACTIONS_PER_HOUR,
    DEFAULT_PER_ACCOUNT_COOLDOWN_HOURS,
    DEFAULT_PER_ACCOUNT_DAILY_CLICK_LIMIT,
    DEFAULT_PER_KEYWORD_ACCOUNT_DAILY_LIMIT,
    DEFAULT_GROUP_IP_DAILY_CLICK_LIMIT,
    DeboostPacingStats,
    account_click_allowed,
    deboost_pacing_window,
)
from app.services.task_center.stats import search_rank_deboost_hourly_execution


pytestmark = pytest.mark.no_postgres


KEYWORD_HASH_A = "a" * 64
KEYWORD_HASH_B = "b" * 64


def _build_engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def _seed_protocol_samples(session: Session, tenant_id: int = 1, bot_username: str = "jisou") -> None:
    """插入达阈值的协议样本。"""
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
    pacing_config: dict | None = None,
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
        pacing_config=pacing_config or {},
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
    target_group_ids: list[int] | None = None,
    exempt_username: str = "exempt_group",
) -> tuple[AccountGroupProxyBinding, list[TgAccount]]:
    session.add(Tenant(id=1, name="默认运营空间"))
    session.add(ProxyAirportSubscription(id=1, tenant_id=1, name="主订阅", enabled=True, sync_status="synced", healthy_node_count=3))
    session.add(AccountPool(id=account_pool_id, tenant_id=1, name="降权分组", pool_purpose="rank_deboost"))
    session.add(ProxyAirportNode(id=proxy_node_id, tenant_id=1, subscription_id=1, node_key=f"node-{proxy_node_id}", status="healthy", observed_exit_ip=observed_exit_ip))
    session.add(AccountProxy(id=30, tenant_id=1, name="rank-runtime", protocol="socks5", host="127.0.0.1", port=1080, status="healthy", alert_status="normal"))
    _seed_protocol_samples(session)

    binding = AccountGroupProxyBinding(
        id=1,
        tenant_id=1,
        account_pool_id=account_pool_id,
        proxy_airport_node_id=proxy_node_id,
        binding_scope="group",
        observed_exit_ip=observed_exit_ip,
        runtime_proxy_id=30,
        status="active",
        bound_by="tester",
    )
    session.add(binding)

    target_ids = target_group_ids or [1001]
    session.add(SearchRankDeboostExemptGroup(
        id=str(uuid4()),
        tenant_id=1,
        task_id="placeholder",  # 在 _make_task 后会更新
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
    """构造 count 个搜索结果，目标在 target_position，豁免在 exempt_position。"""
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
    # 给目标群设置 id 字段以便 compute_deboost_click_targets 通过 id 匹配
    items[target_position - 1]["id"] = 1001
    return items


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
    dwell_seconds_min: int = 10,
    dwell_seconds_max: int = 30,
) -> SearchRankDeboostPayload:
    return SearchRankDeboostPayload(
        bot_username="jisou",
        keyword_hash=keyword_hash,
        keyword_text_ciphertext=keyword_text,
        target_group_ids=target_group_ids or [1001],
        account_pool_id=account_pool_id,
        proxy_airport_node_id=proxy_node_id,
        exempt_group_username=exempt_group_username,
        dwell_seconds_min=dwell_seconds_min,
        dwell_seconds_max=dwell_seconds_max,
        runtime_environment={
            "proxy_egress_guard": "verified",
            "group_proxy_binding_id": str(binding_id),
            "proxy_airport_node_id": str(proxy_node_id),
            "account_pool_id": str(account_pool_id),
            "observed_exit_ip": observed_exit_ip,
        },
    )


# ==================== Planner build_plan 测试 ====================


def test_build_plan_creates_actions_for_rank_deboost_accounts() -> None:
    """Planner 为 rank_deboost 分组中每个账号创建一条 action。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session, account_ids=[100, 101])
        task = _make_task(session)
        # 更新 exempt group 的 task_id
        session.query(SearchRankDeboostExemptGroup).update({SearchRankDeboostExemptGroup.task_id: task.id})
        session.commit()

        created = build_plan(session, task)

        assert created == 2
        actions = session.query(Action).filter_by(task_id=task.id).all()
        assert len(actions) == 2
        assert all(a.action_type == "search_rank_deboost" for a in actions)
        assert all(a.status == "pending" for a in actions)
        reservations = session.query(SearchRankDeboostClickReservation).filter_by(task_id=task.id).all()
        assert len(reservations) == 2
        assert {row.action_id for row in reservations} == {action.id for action in actions}
        assert {row.status for row in reservations} == {"reserved"}
        assert {row.reserved_count for row in reservations} == {1}
        # payload 含稳定的 runtime proxy 合同
        for action in actions:
            runtime = action.payload["runtime_environment"]
            assert runtime["group_proxy_binding_id"] == "1"
            assert runtime["runtime_proxy_id"] == "30"
            assert runtime["binding_generation"] == "1"
            assert action.payload["bot_username"] == "jisou"
            assert len(action.payload["keyword_hash"]) == 64


def test_executor_consumes_reservation_for_confirmed_gateway_outcome() -> None:
    """Gateway confirmed factual outcome 消费 reservation 且只按 outcome 写一条 stat。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        reservation = SearchRankDeboostClickReservation(
            tenant_id=1,
            task_id=task.id,
            action_id=action.id,
            account_id=account.id,
            account_pool_id=10,
            keyword_hash=KEYWORD_HASH_A,
            local_date=_now().date(),
            hour_bucket=_now().replace(minute=0, second=0, microsecond=0),
            expires_at=_now() + timedelta(minutes=15),
        )
        session.add(reservation)
        session.commit()

        gateway_execute = lambda *_args: {
            "success": True,
            "execution_status": "confirmed",
            "observed_exit_ip": "1.1.1.1",
            "search_results": [{"position": 1, "username": "competitor_a"}],
            "click_outcomes": [{"row": 0, "col": 0, "effect": "navigate_only", "status": "confirmed"}],
        }

        result = execute_search_rank_deboost(session, action, account, payload, gateway_execute=gateway_execute)

        assert result["success"] is True
        assert result["execution_status"] == "confirmed"
        session.refresh(reservation)
        assert reservation.status == "consumed"
        assert reservation.consumed_count == 1
        stats = session.query(SearchRankDeboostActionStat).filter_by(action_id=action.id).all()
        assert len(stats) == 1
        assert stats[0].button_effect == "navigate_only"


def test_executor_marks_reservation_unknown_after_click() -> None:
    """unknown_after_click 保留 quota，不写点击 stat，不自动释放重试。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        reservation = SearchRankDeboostClickReservation(
            tenant_id=1,
            task_id=task.id,
            action_id=action.id,
            account_id=account.id,
            account_pool_id=10,
            keyword_hash=KEYWORD_HASH_A,
            local_date=_now().date(),
            hour_bucket=_now().replace(minute=0, second=0, microsecond=0),
            expires_at=_now() + timedelta(minutes=15),
        )
        session.add(reservation)
        session.commit()

        gateway_execute = lambda *_args: {
            "success": False,
            "execution_status": "unknown_after_click",
            "observed_exit_ip": "1.1.1.1",
            "click_outcomes": [{"row": 0, "col": 0, "effect": "navigate_only", "status": "unknown_after_click"}],
        }

        result = execute_search_rank_deboost(session, action, account, payload, gateway_execute=gateway_execute)

        assert result["success"] is False
        assert result["execution_status"] == "unknown_after_click"
        session.refresh(reservation)
        assert reservation.status == "unknown"
        assert reservation.consumed_count == 1
        assert session.query(SearchRankDeboostActionStat).filter_by(action_id=action.id).count() == 0


def test_executor_releases_reservation_for_observed_no_click() -> None:
    """observed_no_click 不消耗 quota，不写点击 stat。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        reservation = SearchRankDeboostClickReservation(
            tenant_id=1,
            task_id=task.id,
            action_id=action.id,
            account_id=account.id,
            account_pool_id=10,
            keyword_hash=KEYWORD_HASH_A,
            local_date=_now().date(),
            hour_bucket=_now().replace(minute=0, second=0, microsecond=0),
            expires_at=_now() + timedelta(minutes=15),
        )
        session.add(reservation)
        session.commit()

        gateway_execute = lambda *_args: {
            "success": False,
            "execution_status": "observed_no_click",
            "observed_exit_ip": "1.1.1.1",
            "click_outcomes": [],
        }

        result = execute_search_rank_deboost(session, action, account, payload, gateway_execute=gateway_execute)

        assert result["success"] is False
        assert result["skip_reason"] == "observed_no_click"
        session.refresh(reservation)
        assert reservation.status == "released"
        assert reservation.consumed_count == 0
        assert session.query(SearchRankDeboostActionStat).filter_by(action_id=action.id).count() == 0


def test_build_plan_blocks_when_protocol_samples_missing() -> None:
    """协议样本不足时 build_plan 返回 0，task.last_error 含 protocol_sample_missing。"""
    engine = _build_engine()
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(ProxyAirportSubscription(id=1, tenant_id=1, name="主订阅", enabled=True, sync_status="synced", healthy_node_count=3))
        session.add(AccountPool(id=10, tenant_id=1, name="降权分组", pool_purpose="rank_deboost"))
        session.add(ProxyAirportNode(id=20, tenant_id=1, subscription_id=1, status="healthy", observed_exit_ip="1.1.1.1"))
        session.add(AccountGroupProxyBinding(id=1, tenant_id=1, account_pool_id=10, proxy_airport_node_id=20, binding_scope="group", status="active", observed_exit_ip="1.1.1.1"))
        # 不插入协议样本
        session.add(TgAccount(id=100, tenant_id=1, pool_id=10, display_name="降权账号", phone_masked="100", status=AccountStatus.ACTIVE.value, account_identity="rank_deboost"))
        task = _make_task(session)
        session.commit()

        created = build_plan(session, task)
        assert created == 0
        assert "协议样本" in (task.last_error or "")
        block_code = (task.stats or {}).get("search_rank_deboost_stats", {}).get("hourly_execution", {}).get("block_code", "")
        assert block_code == "protocol_sample_missing"


def test_build_plan_blocks_when_no_active_group_binding() -> None:
    """分组无 active 代理绑定时 build_plan 返回 0。"""
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session)
        task = _make_task(session)
        session.query(SearchRankDeboostExemptGroup).update({SearchRankDeboostExemptGroup.task_id: task.id})
        # 删除绑定
        session.query(AccountGroupProxyBinding).delete()
        session.commit()

        created = build_plan(session, task)
        assert created == 0
        assert "代理绑定" in (task.last_error or "")
        # block_code 写入 stats
        block_code = (task.stats or {}).get("search_rank_deboost_stats", {}).get("hourly_execution", {}).get("block_code", "")
        assert block_code == "group_proxy_binding_missing"


def test_build_plan_blocks_when_active_binding_has_no_runtime_proxy() -> None:
    """active 分组绑定缺少可执行 runtime proxy 时 Planner fail-closed。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, _accounts = _seed_base(session)
        binding.runtime_proxy_id = None
        task = _make_task(session)
        session.query(SearchRankDeboostExemptGroup).update({SearchRankDeboostExemptGroup.task_id: task.id})
        session.commit()

        created = build_plan(session, task)

        assert created == 0
        assert "runtime proxy" in (task.last_error or "")
        block_code = (task.stats or {}).get("search_rank_deboost_stats", {}).get("hourly_execution", {}).get("block_code", "")
        assert block_code == "group_proxy_runtime_proxy_missing"


def test_build_plan_all_mode_uses_accounts_across_rank_pools() -> None:
    """selection_mode=all 选择所有 enabled rank pool 账号，并按账号所在池绑定代理。"""
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session, account_ids=[100])
        session.add(AccountPool(id=11, tenant_id=1, name="降权分组B", pool_purpose="rank_deboost"))
        session.add(AccountProxy(id=31, tenant_id=1, name="rank-runtime-b", protocol="socks5", host="127.0.0.2", port=1081, status="healthy", alert_status="normal"))
        session.add(ProxyAirportNode(id=21, tenant_id=1, subscription_id=1, node_key="node-21", status="healthy", observed_exit_ip="2.2.2.2"))
        session.add(AccountGroupProxyBinding(
            id=2,
            tenant_id=1,
            account_pool_id=11,
            proxy_airport_node_id=21,
            runtime_proxy_id=31,
            binding_scope="group",
            observed_exit_ip="2.2.2.2",
            status="active",
            bound_by="tester",
        ))
        session.add(TgAccount(
            id=101,
            tenant_id=1,
            pool_id=11,
            display_name="降权账号101",
            phone_masked="101",
            status=AccountStatus.ACTIVE.value,
            account_identity="rank_deboost",
        ))
        task = _make_task(session, config={"max_actions_per_hour": 5})
        task.account_config = {"selection_mode": "all", "max_concurrent": 500}
        session.query(SearchRankDeboostExemptGroup).update({SearchRankDeboostExemptGroup.task_id: task.id})
        session.commit()

        created = build_plan(session, task)

        assert created == 2
        actions = session.query(Action).filter_by(task_id=task.id).order_by(Action.account_id).all()
        assert [action.account_id for action in actions] == [100, 101]
        assert [action.payload["runtime_environment"]["runtime_proxy_id"] for action in actions] == ["30", "31"]
        assert session.query(SearchRankDeboostClickReservation).filter_by(task_id=task.id).count() == 2


def test_build_plan_blocks_when_exempt_group_is_pending_real_search() -> None:
    """随机豁免群仍是占位状态时 Planner 不得创建真实 action。"""
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session, exempt_username="pending_real_search")
        task = _make_task(session)
        session.query(SearchRankDeboostExemptGroup).update({SearchRankDeboostExemptGroup.task_id: task.id})
        session.commit()

        created = build_plan(session, task)

        assert created == 0
        assert "真实搜索结果" in (task.last_error or "")
        actions = session.query(Action).filter_by(task_id=task.id).all()
        assert actions == []
        block_code = (task.stats or {}).get("search_rank_deboost_stats", {}).get("hourly_execution", {}).get("block_code", "")
        assert block_code == "exempt_group_pending_real_search"


def test_build_plan_blocks_when_no_accounts() -> None:
    """分组中无可用账号时 build_plan 返回 0。"""
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session, account_ids=[])  # 无账号
        task = _make_task(session)
        session.query(SearchRankDeboostExemptGroup).update({SearchRankDeboostExemptGroup.task_id: task.id})
        session.commit()

        created = build_plan(session, task)
        assert created == 0
        assert "账号" in (task.last_error or "")
        block_code = (task.stats or {}).get("search_rank_deboost_stats", {}).get("hourly_execution", {}).get("block_code", "")
        assert block_code == "account_unavailable"


def test_build_plan_respects_max_actions_per_hour() -> None:
    """max_actions_per_hour 限制单轮创建 action 数。"""
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session, account_ids=[100, 101, 102])
        task = _make_task(session, config={"max_actions_per_hour": 2})
        session.query(SearchRankDeboostExemptGroup).update({SearchRankDeboostExemptGroup.task_id: task.id})
        session.commit()

        created = build_plan(session, task)
        assert created == 2


# ==================== Executor 测试 ====================


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


def test_executor_skips_when_gateway_unavailable() -> None:
    """gateway 不可用时 skip_reason=rank_observation_gateway_unavailable。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        session.commit()

        result = execute_search_rank_deboost(session, action, account, payload, gateway_execute=None, probe_exit_ip="1.1.1.1")

        assert result["success"] is False
        assert result["skip_reason"] == "rank_observation_gateway_unavailable"


def test_executor_skips_when_proxy_egress_guard_failed() -> None:
    """分组级代理出口校验失败时 skip_reason=proxy_egress_guard_failed。"""
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


def test_executor_skips_when_target_not_in_results() -> None:
    """我方目标群不在搜索结果时 skip_reason=target_not_in_results。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id, target_group_ids=[9999])
        action = _make_action(session, task, account, payload)
        session.commit()

        # 搜索结果中无 id=9999 的群
        search_results = _make_search_results(count=5, target_position=3)
        for item in search_results:
            item["id"] = int(item["peer_id"].lstrip("-"))
        gateway_execute = lambda account_id, payload_data, keyword_text: {"success": True, "search_results": search_results}

        result = execute_search_rank_deboost(session, action, account, payload, gateway_execute=gateway_execute, probe_exit_ip="1.1.1.1")

        assert result["success"] is False
        assert result["skip_reason"] == "target_not_in_results"


def test_executor_skips_when_all_exempt_clicks() -> None:
    """所有结果都被白名单豁免时 skip_reason=all_exempt_clicks。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        session.commit()

        # 只有一个搜索结果，且就是目标群
        search_results = [{"position": 1, "username": "my_target", "peer_id": "-1001", "id": 1001, "title": "目标群", "buttons": []}]
        gateway_execute = lambda account_id, payload_data, keyword_text: {"success": True, "search_results": search_results}

        result = execute_search_rank_deboost(session, action, account, payload, gateway_execute=gateway_execute, probe_exit_ip="1.1.1.1")

        assert result["success"] is False
        assert result["skip_reason"] == "all_exempt_clicks"


def test_executor_clicks_navigate_only_buttons() -> None:
    """竞争群含 navigate_only 按钮时正常点击，写 stats。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        session.commit()

        search_results = _make_search_results(count=5, target_position=3, button_effect="navigate_only")
        gateway_execute = lambda account_id, payload_data, keyword_text: {"success": True, "search_results": search_results}

        result = execute_search_rank_deboost(session, action, account, payload, gateway_execute=gateway_execute, probe_exit_ip="1.1.1.1")

        assert result["success"] is True
        assert result["clicked_count"] == 2  # 位置 1, 2 是竞争群
        stats = session.query(SearchRankDeboostActionStat).filter_by(action_id=action.id).all()
        assert len(stats) == 2
        for stat in stats:
            assert stat.skip_reason == ""
            assert stat.button_effect == "navigate_only"
            assert stat.joined is False
            assert stat.join_button_violation is False
            assert stat.dwell_seconds >= 10
            assert stat.dwell_seconds <= 30
            assert stat.button_hash  # 非空


def test_executor_skips_competitor_with_no_navigable_button() -> None:
    """竞争群只含 external_http_url/unknown 按钮时 skip_reason=no_navigable_button。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        session.commit()

        search_results = _make_search_results(count=5, target_position=3, button_effect="external")
        gateway_execute = lambda account_id, payload_data, keyword_text: {"success": True, "search_results": search_results}

        result = execute_search_rank_deboost(session, action, account, payload, gateway_execute=gateway_execute, probe_exit_ip="1.1.1.1")

        # action 仍成功（点击范围正确，只是竞争群无 navigable button）
        assert result["success"] is True
        assert result["clicked_count"] == 0
        stats = session.query(SearchRankDeboostActionStat).filter_by(action_id=action.id).all()
        assert len(stats) == 2  # 两个竞争群都写了 stat
        for stat in stats:
            assert stat.skip_reason == "no_navigable_button"


def test_executor_navigate_only_no_join_click_when_join_candidate_present() -> None:
    """竞争群含 join_candidate 按钮时只点 navigate_only 按钮，不点加入按钮。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        session.commit()

        # 构造搜索结果：竞争群同时含 navigate_only 和 join_candidate 按钮
        search_results = _make_search_results(count=5, target_position=3, button_effect="navigate_only")
        for item in search_results:
            if item["position"] in (1, 2):  # 竞争群
                item["buttons"] = [
                    {"text": "加入", "url": "https://t.me/joinchat/abc", "effect": "join_candidate", "position": 1},
                    {"text": "详情", "url": "https://example.com", "effect": "navigate_only", "position": 2},
                ]
        gateway_execute = lambda account_id, payload_data, keyword_text: {"success": True, "search_results": search_results}

        result = execute_search_rank_deboost(session, action, account, payload, gateway_execute=gateway_execute, probe_exit_ip="1.1.1.1")

        assert result["success"] is True
        assert result["clicked_count"] == 2
        stats = session.query(SearchRankDeboostActionStat).filter_by(action_id=action.id).all()
        assert len(stats) == 2
        for stat in stats:
            # 应点击 navigate_only，不点 join_candidate
            assert stat.button_effect == "navigate_only"
            assert stat.joined is False
            assert stat.join_button_detected is True  # 检测到了 join_candidate 按钮
            assert stat.join_button_violation is False


def test_executor_join_button_violation_directly() -> None:
    """直接构造 join_button_violation 场景：竞争群按钮被标记为 navigate_only 但实际是 join_candidate。

    通过 monkeypatch NAVIGABLE_BUTTON_EFFECTS 让 join_candidate 也被视为可点击，触发 violation 自检。
    """
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

        # 让 join_candidate 也进入 navigable 集合，触发 violation 自检
        original_navigable = executor_module.NAVIGABLE_BUTTON_EFFECTS
        executor_module.NAVIGABLE_BUTTON_EFFECTS = {"navigate_only", "join_candidate"}
        try:
            result = execute_search_rank_deboost(session, action, account, payload, gateway_execute=gateway_execute, probe_exit_ip="1.1.1.1")
        finally:
            executor_module.NAVIGABLE_BUTTON_EFFECTS = original_navigable

        assert result["success"] is False
        assert result["join_button_violation"] is True
        assert result["error_code"] == "join_button_violation"
        # 账号应被暂停
        session.refresh(account)
        assert account.status == AccountStatus.LIMITED.value
        # 应写了 violation stat
        stats = session.query(SearchRankDeboostActionStat).filter_by(action_id=action.id).all()
        violation_stats = [s for s in stats if s.join_button_violation]
        assert len(violation_stats) >= 1


def test_executor_stats_record_correct_fields() -> None:
    """stats 记录含 button_hash、position、effect、dwell_seconds、joined=false、join_button_detected。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id, dwell_seconds_min=15, dwell_seconds_max=20)
        action = _make_action(session, task, account, payload)
        session.commit()

        search_results = _make_search_results(count=5, target_position=3, button_effect="navigate_only")
        gateway_execute = lambda account_id, payload_data, keyword_text: {"success": True, "search_results": search_results}

        result = execute_search_rank_deboost(session, action, account, payload, gateway_execute=gateway_execute, probe_exit_ip="1.1.1.1")

        assert result["success"] is True
        stats = session.query(SearchRankDeboostActionStat).filter_by(action_id=action.id).all()
        assert len(stats) == 2
        for stat in stats:
            assert stat.button_hash  # 非空
            assert stat.competitor_position in (1, 2)
            assert stat.button_effect == "navigate_only"
            assert 15 <= stat.dwell_seconds <= 20
            assert stat.joined is False
            assert stat.join_button_detected is False
            assert stat.join_button_violation is False
            assert stat.account_pool_id == 10
            assert stat.proxy_airport_node_id == 20
            assert stat.bot_username == "jisou"
            assert stat.keyword_hash == KEYWORD_HASH_A


# ==================== Dispatcher 集成测试 ====================


def test_dispatch_action_skips_when_gateway_unavailable() -> None:
    """dispatch_action 处理 search_rank_deboost action，gateway 不可用时 skip。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        session.commit()

        result = dispatch_action(session, action)

        assert result is True
        assert action.status == "skipped"
        assert action.result["skip_reason"] == "rank_observation_gateway_unavailable"


def test_dispatch_action_skips_when_proxy_egress_drift(monkeypatch) -> None:
    """dispatch_action 在 gateway 本次出口探测缺失时 skip with proxy_egress_guard_failed。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session, observed_exit_ip="1.1.1.1")
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        session.commit()

        search_results = _make_search_results(count=5, target_position=3, button_effect="navigate_only")

        def fake_gateway(_account_id, _payload_data, _keyword_text):
            return {"success": True, "search_results": search_results}

        from app.services import _common

        monkeypatch.setattr(_common.gateway, "execute_search_rank_deboost", fake_gateway, raising=False)
        result = dispatch_action(session, action)

        assert result is True
        assert action.status == "skipped"
        assert action.result["error_code"] == "proxy_egress_guard_failed"


def test_dispatch_action_uses_gateway_probe_instead_of_stored_binding_ip(monkeypatch) -> None:
    """Dispatcher 不得拿 binding.observed_exit_ip 伪装成本次出口探测。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session, observed_exit_ip="1.1.1.1")
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        session.commit()

        search_results = _make_search_results(count=5, target_position=3, button_effect="navigate_only")

        def fake_gateway(_account_id, _payload_data, _keyword_text):
            return {
                "success": True,
                "observed_exit_ip": "9.9.9.9",
                "search_results": search_results,
            }

        from app.services import _common

        monkeypatch.setattr(_common.gateway, "execute_search_rank_deboost", fake_gateway, raising=False)

        result = dispatch_action(session, action)

        assert result is True
        assert action.status == "skipped"
        assert action.result["error_code"] == "proxy_egress_guard_failed"
        assert session.query(SearchRankDeboostActionStat).filter_by(action_id=action.id).count() == 0


# ==================== Pacing 限流测试 ====================


def test_pacing_per_account_daily_click_limit() -> None:
    """单账号当日点击数达上限时 pacing 阻断。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session, config={"per_account_daily_click_limit": 1})
        account = accounts[0]
        session.commit()

        # 预置 1 条已点击 stat
        session.add(SearchRankDeboostActionStat(
            id=str(uuid4()),
            tenant_id=1,
            task_id=task.id,
            action_id="prev-action",
            account_id=account.id,
            account_pool_id=10,
            proxy_airport_node_id=20,
            bot_username="jisou",
            keyword_hash=KEYWORD_HASH_A,
            competitor_group_username="competitor",
            competitor_position=1,
            button_hash="hash",
            button_effect="navigate_only",
            dwell_seconds=15,
            hour_bucket=_now().replace(minute=0, second=0, microsecond=0),
            captured_at=_now(),
            skip_reason="",
        ))
        session.commit()

        window = deboost_pacing_window(task, _now())
        stats = DeboostPacingStats(tenant_timezone="Asia/Shanghai", local_date=window.local_date.isoformat())
        allowed = account_click_allowed(session, task, account.id, KEYWORD_HASH_B, 10, window, stats)

        assert allowed is False
        assert "per_account_daily_click_limit_reached" in stats.last_limit_reason


@pytest.mark.parametrize(
    ("reservation_status", "expected_allowed"),
    [
        ("reserved", False),
        ("unknown", False),
        ("released", True),
    ],
)
def test_pacing_counts_active_reservations_for_daily_limit(reservation_status: str, expected_allowed: bool) -> None:
    """reserved/unknown 占每日额度，released 不占。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session, config={"per_account_daily_click_limit": 1})
        account = accounts[0]
        now_value = _now()
        session.add(SearchRankDeboostClickReservation(
            tenant_id=1,
            task_id=task.id,
            action_id="reserved-action",
            account_id=account.id,
            account_pool_id=10,
            keyword_hash=KEYWORD_HASH_A,
            local_date=now_value.date(),
            hour_bucket=now_value.replace(minute=0, second=0, microsecond=0),
            reserved_count=1,
            consumed_count=1 if reservation_status == "unknown" else 0,
            status=reservation_status,
            expires_at=now_value + timedelta(minutes=15),
        ))
        session.commit()

        window = deboost_pacing_window(task, now_value)
        stats = DeboostPacingStats(tenant_timezone="Asia/Shanghai", local_date=window.local_date.isoformat())
        allowed = account_click_allowed(session, task, account.id, KEYWORD_HASH_B, 10, window, stats)

        assert allowed is expected_allowed


def test_pacing_does_not_double_count_reserved_action_stat() -> None:
    """confirmed 后 reservation 与同 action stat 同时存在时只计一次。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session, config={"per_account_daily_click_limit": 2})
        account = accounts[0]
        now_value = _now()
        session.add(SearchRankDeboostClickReservation(
            tenant_id=1,
            task_id=task.id,
            action_id="confirmed-action",
            account_id=account.id,
            account_pool_id=10,
            keyword_hash=KEYWORD_HASH_A,
            local_date=now_value.date(),
            hour_bucket=now_value.replace(minute=0, second=0, microsecond=0),
            reserved_count=1,
            consumed_count=1,
            status="consumed",
            expires_at=now_value + timedelta(minutes=15),
        ))
        session.add(SearchRankDeboostActionStat(
            id=str(uuid4()),
            tenant_id=1,
            task_id=task.id,
            action_id="confirmed-action",
            account_id=account.id,
            account_pool_id=10,
            proxy_airport_node_id=20,
            bot_username="jisou",
            keyword_hash=KEYWORD_HASH_A,
            competitor_group_username="competitor",
            competitor_position=1,
            button_hash="hash",
            button_effect="navigate_only",
            dwell_seconds=15,
            hour_bucket=now_value.replace(minute=0, second=0, microsecond=0),
            captured_at=now_value,
            skip_reason="",
        ))
        session.commit()

        window = deboost_pacing_window(task, now_value)
        stats = DeboostPacingStats(tenant_timezone="Asia/Shanghai", local_date=window.local_date.isoformat())
        allowed = account_click_allowed(session, task, account.id, KEYWORD_HASH_B, 10, window, stats)

        assert allowed is True


def test_pacing_per_keyword_account_daily_limit() -> None:
    """单账号每关键词每日上限阻断。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session, config={"per_keyword_account_daily_limit": 1})
        account = accounts[0]
        session.commit()

        session.add(SearchRankDeboostActionStat(
            id=str(uuid4()),
            tenant_id=1,
            task_id=task.id,
            action_id="prev-action",
            account_id=account.id,
            account_pool_id=10,
            proxy_airport_node_id=20,
            bot_username="jisou",
            keyword_hash=KEYWORD_HASH_A,
            competitor_group_username="competitor",
            competitor_position=1,
            button_hash="hash",
            button_effect="navigate_only",
            dwell_seconds=15,
            hour_bucket=_now().replace(minute=0, second=0, microsecond=0),
            captured_at=_now(),
            skip_reason="",
        ))
        session.commit()

        window = deboost_pacing_window(task, _now())
        stats = DeboostPacingStats(tenant_timezone="Asia/Shanghai", local_date=window.local_date.isoformat())
        # 同关键词 → 阻断
        allowed_same = account_click_allowed(session, task, account.id, KEYWORD_HASH_A, 10, window, stats)
        assert allowed_same is False

        # 不同关键词 → 放行（未达 per_account_daily_click_limit 默认 5）
        stats2 = DeboostPacingStats(tenant_timezone="Asia/Shanghai", local_date=window.local_date.isoformat())
        allowed_diff = account_click_allowed(session, task, account.id, KEYWORD_HASH_B, 10, window, stats2)
        assert allowed_diff is True


def test_pacing_group_ip_daily_click_limit() -> None:
    """单分组共享出口 IP 每日上限阻断。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session, account_ids=[100, 101])
        task = _make_task(session, config={"group_ip_daily_click_limit": 1})
        session.commit()

        # 账号 100 已点击 1 次
        session.add(SearchRankDeboostActionStat(
            id=str(uuid4()),
            tenant_id=1,
            task_id=task.id,
            action_id="prev-action",
            account_id=100,
            account_pool_id=10,
            proxy_airport_node_id=20,
            bot_username="jisou",
            keyword_hash=KEYWORD_HASH_A,
            competitor_group_username="competitor",
            competitor_position=1,
            button_hash="hash",
            button_effect="navigate_only",
            dwell_seconds=15,
            hour_bucket=_now().replace(minute=0, second=0, microsecond=0),
            captured_at=_now(),
            skip_reason="",
        ))
        session.commit()

        window = deboost_pacing_window(task, _now())
        stats = DeboostPacingStats(tenant_timezone="Asia/Shanghai", local_date=window.local_date.isoformat())
        # 账号 101 也应被阻断（分组 IP 上限已达）
        allowed = account_click_allowed(session, task, 101, KEYWORD_HASH_A, 10, window, stats)
        assert allowed is False
        assert "group_ip_daily_click_limit_reached" in stats.last_limit_reason


def test_pacing_task_hourly_limit() -> None:
    """任务每小时上限阻断。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session, config={"max_actions_per_hour": 1, "per_account_cooldown_hours": 0})
        account = accounts[0]
        session.commit()

        # 预置 1 条当前小时的 action
        from app.models import Action

        session.add(Action(
            id=str(uuid4()),
            tenant_id=1,
            task_id=task.id,
            task_type="search_rank_deboost",
            action_type="search_rank_deboost",
            account_id=account.id,
            scheduled_at=_now(),
            status="success",
        ))
        session.commit()

        window = deboost_pacing_window(task, _now())
        stats = DeboostPacingStats(tenant_timezone="Asia/Shanghai", local_date=window.local_date.isoformat())
        allowed = account_click_allowed(session, task, account.id, KEYWORD_HASH_B, 10, window, stats)
        assert allowed is False
        assert "task_hourly_limit_reached" in stats.last_limit_reason


def test_pacing_per_account_cooldown_hours() -> None:
    """账号冷却期内阻断。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session, config={"per_account_cooldown_hours": 4})
        account = accounts[0]
        session.commit()

        from app.models import Action

        # 预置一条最近执行的 action（1 小时前）
        recent_at = _now() - timedelta(hours=1)
        session.add(Action(
            id=str(uuid4()),
            tenant_id=1,
            task_id=task.id,
            task_type="search_rank_deboost",
            action_type="search_rank_deboost",
            account_id=account.id,
            scheduled_at=recent_at,
            executed_at=recent_at,
            status="success",
        ))
        session.commit()

        window = deboost_pacing_window(task, _now())
        stats = DeboostPacingStats(tenant_timezone="Asia/Shanghai", local_date=window.local_date.isoformat())
        allowed = account_click_allowed(session, task, account.id, KEYWORD_HASH_A, 10, window, stats)
        assert allowed is False
        assert "per_account_cooldown_active" in stats.last_limit_reason


# ==================== Stats 聚合测试 ====================


def test_search_rank_deboost_hourly_execution_aggregates() -> None:
    """search_rank_deboost_hourly_execution 聚合 action 与 click stat。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session, config={"max_actions_per_hour": 5})
        account = accounts[0]
        session.commit()

        from app.models import Action

        # 1 条成功 action
        session.add(Action(
            id=str(uuid4()),
            tenant_id=1,
            task_id=task.id,
            task_type="search_rank_deboost",
            action_type="search_rank_deboost",
            account_id=account.id,
            scheduled_at=_now(),
            executed_at=_now(),
            status="success",
        ))
        # 2 条点击 stat
        for position in (1, 2):
            session.add(SearchRankDeboostActionStat(
                id=str(uuid4()),
                tenant_id=1,
                task_id=task.id,
                action_id="action-1",
                account_id=account.id,
                account_pool_id=10,
                proxy_airport_node_id=20,
                bot_username="jisou",
                keyword_hash=KEYWORD_HASH_A,
                competitor_group_username=f"competitor_{position}",
                competitor_position=position,
                button_hash=f"hash_{position}",
                button_effect="navigate_only",
                dwell_seconds=15,
                hour_bucket=_now().replace(minute=0, second=0, microsecond=0),
                captured_at=_now(),
                skip_reason="",
            ))
        session.commit()

        hourly = search_rank_deboost_hourly_execution(session, task, _now())

        assert hourly["max_actions_per_hour"] == 5
        assert hourly["success_count"] == 1
        assert hourly["hourly_click_count"] == 2
        assert hourly["capacity"] == 4  # 5 - 1 - 0 - 0
        assert hourly["status"] == "catching_up"


def test_search_rank_deboost_hourly_execution_blocked_when_capacity_zero() -> None:
    """capacity=0 且 success < goal 时 status=blocked（overdue pending 占满 capacity）。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session, config={"max_actions_per_hour": 1})
        account = accounts[0]
        session.commit()

        from app.models import Action

        # 1 条 overdue pending action 占满 capacity，success=0 < goal=1 → blocked
        session.add(Action(
            id=str(uuid4()),
            tenant_id=1,
            task_id=task.id,
            task_type="search_rank_deboost",
            action_type="search_rank_deboost",
            account_id=account.id,
            scheduled_at=_now() - timedelta(minutes=30),  # 过期未执行
            status="pending",
        ))
        session.commit()

        hourly = search_rank_deboost_hourly_execution(session, task, _now())
        assert hourly["capacity"] == 0
        assert hourly["status"] == "blocked"
