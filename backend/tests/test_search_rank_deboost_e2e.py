"""search_rank_deboost 端到端全链路测试（Task 21.1）。

覆盖 spec 场景：
- 任务创建 → 预检通过（service 层 create_search_rank_deboost_task）
- 样本采集门控：协议样本不足时创建被拒
- 真实执行（mock Gateway）：build_plan → execute → 写 SearchRankDeboostActionStat
- stats 写入：task.stats 含 search_rank_deboost_stats.hourly_execution
- 详情查询：get_task_detail 返回降权任务状态与 stats
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
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
    OperationTarget,
    ProxyAirportNode,
    ProxyAirportSubscription,
    Task,
    TelegramDeveloperApp,
    Tenant,
    TgAccount,
)
from app.models.search_rank_deboost import (
    AccountGroupProxyBinding,
    SearchRankDeboostActionStat,
    SearchRankDeboostClickReservation,
    SearchRankDeboostExemptGroup,
)
from app.schemas.task_center import SearchRankDeboostSimpleTaskCreate, SearchRankDeboostTaskCreate
from app.security import decrypt_secret, encrypt_secret, encrypt_session
from app.services._common import _now
from app.services.task_center.executors.search_rank_deboost import (
    build_plan,
    execute_search_rank_deboost,
)
from app.services.task_center.service import (
    create_and_start_search_rank_deboost_task,
    create_search_rank_deboost_task,
    create_simple_search_rank_deboost_task,
    get_task_detail,
    reroll_search_rank_deboost_exempt_group,
    start_task,
)


pytestmark = pytest.mark.no_postgres


KEYWORD_HASH_A = "a" * 64
SIMPLE_SEARCH_CLICK_END = datetime(2030, 1, 1, tzinfo=timezone.utc)


def _build_engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def _simple_search_click_payload(**overrides) -> SearchRankDeboostSimpleTaskCreate:
    data = {
        "target_title": "我方目标群",
        "target_link": "https://t.me/my_target",
        "keywords": ["关键词A"],
        "target_count": 1,
        "account_group_id": 10,
        "max_actions_per_day": 5,
        "scheduled_end": SIMPLE_SEARCH_CLICK_END,
        "daily_jitter_percent": 20,
        "hourly_jitter_percent": 30,
    }
    data.update(overrides)
    return SearchRankDeboostSimpleTaskCreate(**data)


def _seed_protocol_samples(session: Session, tenant_id: int = 1, bot_username: str = "jisou") -> None:
    """插入达阈值的协议样本。"""
    for _ in range(2):
        session.add(BotProtocolSample(
            tenant_id=tenant_id, bot_username=bot_username,
            sample_type="start_response", sample_purpose="rank_deboost", is_active=True,
        ))
    for _ in range(5):
        session.add(BotProtocolSample(
            tenant_id=tenant_id, bot_username=bot_username,
            sample_type="search_results", sample_purpose="rank_deboost", is_active=True,
        ))
    for _ in range(3):
        session.add(BotProtocolSample(
            tenant_id=tenant_id, bot_username=bot_username,
            sample_type="pagination_response", sample_purpose="rank_deboost", is_active=True,
        ))
    for effect in ("navigate_only", "join_candidate", "external_http_url"):
        session.add(BotProtocolSample(
            tenant_id=tenant_id, bot_username=bot_username,
            sample_type="button_structure", sample_purpose="rank_deboost", is_active=True,
            structure_json={"button_effect": effect},
        ))
    for _ in range(3):
        session.add(BotProtocolSample(
            tenant_id=tenant_id, bot_username=bot_username,
            sample_type="exit_ip_observation", sample_purpose="rank_deboost", is_active=True,
        ))


def _seed_base(
    session: Session,
    *,
    account_pool_id: int = 10,
    proxy_node_id: int = 20,
    observed_exit_ip: str = "1.1.1.1",
    account_ids: list[int] | None = None,
    with_samples: bool = True,
    with_binding: bool = True,
) -> tuple[AccountGroupProxyBinding | None, list[TgAccount]]:
    session.add(Tenant(id=1, name="默认运营空间"))
    session.add(ProxyAirportSubscription(
        id=1, tenant_id=1, name="主订阅", enabled=True, sync_status="synced", healthy_node_count=3,
    ))
    session.add(AccountPool(id=account_pool_id, tenant_id=1, name="降权分组", pool_purpose="rank_deboost"))
    session.add(OperationTarget(
        id=1001,
        tenant_id=1,
        target_type="group",
        tg_peer_id="-1001",
        title="我方目标群",
        username="my_target",
    ))
    session.add(ProxyAirportNode(
        id=proxy_node_id, tenant_id=1, subscription_id=1, node_key=f"node-{proxy_node_id}",
        status="healthy", observed_exit_ip=observed_exit_ip,
        protocol="socks5", proxy_host="127.0.0.1", proxy_port=1080,
    ))
    session.add(AccountProxy(id=30, tenant_id=1, name="rank-runtime", protocol="socks5", host="127.0.0.1", port=1080, status="healthy", alert_status="normal"))
    session.add(TelegramDeveloperApp(id=40, app_name="rank-app", api_id=12345, api_hash_ciphertext=encrypt_secret("rank-api-hash")))
    if with_samples:
        _seed_protocol_samples(session)

    binding: AccountGroupProxyBinding | None = None
    if with_binding:
        binding = AccountGroupProxyBinding(
            id=1, tenant_id=1, account_pool_id=account_pool_id, proxy_airport_node_id=proxy_node_id,
            runtime_proxy_id=30, binding_scope="group", observed_exit_ip=observed_exit_ip, status="active", bound_by="tester",
        )
        session.add(binding)

    accounts: list[TgAccount] = []
    for account_id in (account_ids if account_ids is not None else [100]):
        account = TgAccount(
            id=account_id, tenant_id=1, pool_id=account_pool_id,
            display_name=f"降权账号{account_id}", phone_masked=str(account_id),
            status=AccountStatus.ACTIVE.value, account_identity="rank_deboost", health_score=95,
            developer_app_id=40,
            developer_app_version=1,
            session_ciphertext=encrypt_session(f"rank-session-{account_id}"),
        )
        session.add(account)
        accounts.append(account)

    session.flush()
    return binding, accounts


# ==================== 1. 任务创建 → 预检通过 ====================


def test_e2e_create_task_precheck_passes() -> None:
    """创建降权任务：分组+账号+协议样本+代理绑定齐备 → 预检通过，task 落库。"""
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session, account_ids=[100], with_binding=False)  # service 会自建 binding
        session.commit()

        payload = SearchRankDeboostTaskCreate(
            name="E2E降权任务",
            search_bots=["jisou"],
            keywords=[{"text": "关键词A"}],
            target_group_ids=[1001],
            account_pool_id=10,
            proxy_airport_node_id=20,
            config={"per_account_daily_click_limit": 5},
            notes="",
        )
        task = create_search_rank_deboost_task(session, 1, payload, operator="tester")

        assert task.type == "search_rank_deboost"
        assert task.status == "draft"
        assert task.priority == 3
        assert task.timezone == "Asia/Shanghai"
        # type_config 含基本字段
        assert task.type_config["search_bots"] == ["jisou"]
        assert task.type_config["target_group_ids"] == [1001]
        assert task.type_config["account_pool_id"] == 10
        assert task.type_config["proxy_airport_node_id"] == 20
        # 分组级代理绑定已创建（service 内部创建独立 binding 记录）
        bindings = session.query(AccountGroupProxyBinding).filter_by(
            tenant_id=1, account_pool_id=10, status="active",
        ).all()
        assert len(bindings) >= 1
        assert bindings[0].observed_exit_ip == "1.1.1.1"
        # 预选豁免群已写入（search_results=None 时占位）
        exempt = session.query(SearchRankDeboostExemptGroup).filter_by(task_id=task.id).one()
        assert exempt.exempt_group_username  # 占位或实际用户名
        assert exempt.selected_by == "tester"


def test_e2e_create_task_uses_account_config_and_existing_group_bindings() -> None:
    """新前端只提交 account_config；创建阶段按已绑定 rank 分组校验 readiness。"""
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session, account_ids=[100], with_binding=True)
        session.commit()

        payload = SearchRankDeboostTaskCreate(
            name="全黑账号组降权任务",
            search_bots=["jisou"],
            keywords=[{"text": "关键词A"}],
            target_group_ids=[1001],
            account_config={"selection_mode": "all", "max_concurrent": 500},
        )
        task = create_search_rank_deboost_task(session, 1, payload, operator="tester")

        assert task.status == "draft"
        assert task.account_config["selection_mode"] == "all"
        assert task.type_config["account_pool_id"] == 10
        assert task.type_config["proxy_airport_node_id"] == 20
        assert session.query(AccountGroupProxyBinding).filter_by(status="active").count() == 1


def test_e2e_create_task_rejects_node_used_by_other_group_binding() -> None:
    """创建降权任务必须复用分组代理绑定服务，拒绝复用其他分组 active 节点。"""
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session, account_ids=[100], with_binding=False)
        session.add(AccountPool(id=11, tenant_id=1, name="其他降权分组", pool_purpose="rank_deboost"))
        session.add(AccountGroupProxyBinding(
            tenant_id=1,
            account_pool_id=11,
            proxy_airport_node_id=20,
            binding_scope="group",
            observed_exit_ip="1.1.1.1",
            status="active",
            bound_by="tester",
        ))
        session.commit()

        payload = SearchRankDeboostTaskCreate(
            name="节点复用任务",
            search_bots=["jisou"],
            keywords=[{"text": "关键词A"}],
            target_group_ids=[1001],
            account_pool_id=10,
            proxy_airport_node_id=20,
        )
        with pytest.raises(ValueError, match="已被其他降权分组绑定"):
            create_search_rank_deboost_task(session, 1, payload, operator="tester")


def test_e2e_start_rejects_pending_real_search_exempt_group(monkeypatch) -> None:
    """草稿排名观察任务只有拿到真实豁免群后才能启动。"""
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session, account_ids=[100], with_binding=False)
        session.commit()

        from app.services import _common

        monkeypatch.setattr(
            _common.gateway,
            "execute_search_rank_deboost",
            lambda *_args, **_kwargs: {"success": True, "search_results": [], "observed_exit_ip": "1.1.1.1"},
            raising=False,
        )
        monkeypatch.setattr(
            _common.gateway,
            "search_rank_deboost_candidates",
            lambda *_args, **_kwargs: {"success": False, "error_code": "candidate_unavailable"},
        )
        monkeypatch.setattr(_common.gateway, "supports_rank_deboost_observation", True)

        payload = SearchRankDeboostTaskCreate(
            name="草稿启动任务",
            search_bots=["jisou"],
            keywords=[{"text": "关键词A"}],
            target_group_ids=[1001],
            account_pool_id=10,
            proxy_airport_node_id=20,
        )
        task = create_search_rank_deboost_task(session, 1, payload, operator="tester")
        assert task.status == "draft"

        with pytest.raises(ValueError, match="真实搜索候选源"):
            start_task(session, 1, task.id, actor="tester")

        session.refresh(task)
        assert task.status == "draft"
        assert task.next_run_at is None
        readiness = task.stats["rank_deboost_readiness"]
        assert readiness["status"] == "blocked"
        assert readiness["blocker"] == "搜索排名观察真实搜索候选源返回格式无效"
        assert readiness["checked_at"]
        assert readiness["evidence_summary"] == "rank_start_preparation"


def test_e2e_start_prepares_pending_exempt_group_for_simple_rank_draft(monkeypatch) -> None:
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session, account_ids=[100], with_binding=True)
        session.commit()
        task = create_simple_search_rank_deboost_task(
            session,
            1,
            _simple_search_click_payload(),
            operator="tester",
        )

        from app.services import _common

        monkeypatch.setattr(
            _common.gateway,
            "search_rank_deboost_candidates",
            lambda *_args, **_kwargs: {
                "success": True,
                "execution_status": "candidates_found",
                "search_results": [{"username": "real_exempt", "peer_id": "-100999", "title": "真实豁免群"}],
            },
        )
        monkeypatch.setattr(
            _common.gateway,
            "execute_search_rank_deboost",
            lambda *_args, **_kwargs: {"success": True, "observed_exit_ip": "1.1.1.1"},
        )
        monkeypatch.setattr(_common.gateway, "supports_rank_deboost_observation", True)
        executed_statements = []
        original_execute = session.execute

        def capture_execute(statement, *args, **kwargs):
            executed_statements.append(statement)
            return original_execute(statement, *args, **kwargs)

        monkeypatch.setattr(session, "execute", capture_execute)

        started = start_task(session, 1, task.id, actor="tester")

        exempt = session.query(SearchRankDeboostExemptGroup).filter_by(task_id=task.id).one()
        assert started.status == "running"
        assert exempt.exempt_group_username == "real_exempt"
        assert exempt.selected_by == "tester"
        readiness = started.stats["rank_deboost_readiness"]
        assert readiness["status"] == "ready"
        assert readiness["checked_at"]
        assert readiness["evidence_summary"] == "rank_start_preparation"
        assert "blocker" not in readiness
        assert any(getattr(statement, "_for_update_arg", None) is not None for statement in executed_statements)


def test_e2e_start_records_candidate_gateway_exception_as_readiness_blocker(monkeypatch) -> None:
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session, account_ids=[100], with_binding=True)
        session.commit()
        task = create_simple_search_rank_deboost_task(
            session,
            1,
            _simple_search_click_payload(),
            operator="tester",
        )

        from app.services import _common

        def raise_transport_error(*_args, **_kwargs):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(_common.gateway, "search_rank_deboost_candidates", raise_transport_error)
        monkeypatch.setattr(_common.gateway, "supports_rank_deboost_observation", True)

        with pytest.raises(ValueError, match="真实候选搜索失败"):
            start_task(session, 1, task.id, actor="tester")

        session.refresh(task)
        assert task.status == "draft"
        assert task.stats["rank_deboost_readiness"]["blocker"] == "搜索排名观察真实候选搜索失败：RuntimeError"


def test_e2e_create_and_start_rolls_back_pending_real_search_failure(monkeypatch) -> None:
    """创建并启动失败时不能留下草稿、豁免群、动作、reservation 或分组绑定。"""
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session, account_ids=[100], with_binding=False)
        session.commit()

        from app.services import _common

        monkeypatch.setattr(
            _common.gateway,
            "execute_search_rank_deboost",
            lambda *_args, **_kwargs: {"success": True, "search_results": [], "observed_exit_ip": "1.1.1.1"},
            raising=False,
        )
        monkeypatch.setattr(
            _common.gateway,
            "search_rank_deboost_candidates",
            lambda *_args, **_kwargs: {"success": False, "error_code": "candidate_unavailable"},
        )
        monkeypatch.setattr(_common.gateway, "supports_rank_deboost_observation", True)

        payload = SearchRankDeboostTaskCreate(
            name="原子启动失败任务",
            search_bots=["jisou"],
            keywords=[{"text": "关键词A"}],
            target_group_ids=[1001],
            account_pool_id=10,
            proxy_airport_node_id=20,
        )
        with pytest.raises(ValueError, match="真实搜索候选源"):
            create_and_start_search_rank_deboost_task(session, 1, payload, operator="tester")

        assert session.query(Task).filter_by(name="原子启动失败任务").count() == 0
        assert session.query(SearchRankDeboostExemptGroup).count() == 0
        assert session.query(AccountGroupProxyBinding).count() == 0
        assert session.query(Action).count() == 0
        assert session.query(SearchRankDeboostClickReservation).count() == 0


def test_e2e_reroll_rejects_when_real_search_provider_missing() -> None:
    """未接入真实搜索候选源时，重选不能继续写 pending_real_search 后返回成功。"""
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session, account_ids=[100], with_binding=False)
        session.commit()

        payload = SearchRankDeboostTaskCreate(
            name="重选缺少真实候选源",
            search_bots=["jisou"],
            keywords=[{"text": "关键词A"}],
            target_group_ids=[1001],
            account_pool_id=10,
            proxy_airport_node_id=20,
        )
        task = create_search_rank_deboost_task(session, 1, payload, operator="tester")
        before = session.query(SearchRankDeboostExemptGroup).filter_by(task_id=task.id).one()
        assert before.exempt_group_username == "pending_real_search"

        with pytest.raises(ValueError, match="真实搜索候选源"):
            reroll_search_rank_deboost_exempt_group(session, 1, task.id, operator="tester")

        after = session.query(SearchRankDeboostExemptGroup).filter_by(task_id=task.id).one()
        assert after.exempt_group_username == "pending_real_search"
        assert after.previous_exempt_group_username == ""


def test_e2e_start_rejects_when_rank_observation_gateway_missing() -> None:
    """真实执行 gateway 未接入时不能把排名观察任务启动成 running。"""
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session, account_ids=[100], with_binding=False)
        session.commit()

        payload = SearchRankDeboostTaskCreate(
            name="无 gateway 启动任务",
            search_bots=["jisou"],
            keywords=[{"text": "关键词A"}],
            target_group_ids=[1001],
            account_pool_id=10,
            proxy_airport_node_id=20,
        )
        task = create_search_rank_deboost_task(session, 1, payload, operator="tester")
        exempt = session.query(SearchRankDeboostExemptGroup).filter_by(task_id=task.id).one()
        exempt.exempt_group_username = "real_exempt"
        exempt.exempt_group_peer_id = "-100999"
        exempt.exempt_group_title = "真实豁免群"
        session.commit()

        with pytest.raises(ValueError, match="gateway"):
            start_task(session, 1, task.id, actor="tester")


def test_e2e_start_rejects_unresolvable_target_identity_before_gateway_check() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session, account_ids=[100], with_binding=False)
        session.commit()

        payload = SearchRankDeboostTaskCreate(
            name="无可验证目标任务",
            search_bots=["jisou"],
            keywords=[{"text": "关键词A"}],
            target_group_ids=[9999],
            account_pool_id=10,
            proxy_airport_node_id=20,
        )
        task = create_search_rank_deboost_task(session, 1, payload, operator="tester")
        exempt = session.query(SearchRankDeboostExemptGroup).filter_by(task_id=task.id).one()
        exempt.exempt_group_username = "real_exempt"
        exempt.exempt_group_peer_id = "-100999"
        session.commit()

        with pytest.raises(ValueError, match="目标群缺少可验证"):
            start_task(session, 1, task.id, actor="tester")


# ==================== 2. 样本采集门控 ====================


def test_e2e_create_task_rejected_when_protocol_samples_missing() -> None:
    """协议样本不足时创建降权任务被拒（ValueError 含协议样本缺口）。"""
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session, account_ids=[100], with_samples=False, with_binding=False)  # 不采集样本
        session.commit()

        payload = SearchRankDeboostTaskCreate(
            name="无样本任务",
            search_bots=["jisou"],
            keywords=[{"text": "关键词A"}],
            target_group_ids=[1001],
            account_pool_id=10,
            proxy_airport_node_id=20,
        )
        with pytest.raises(ValueError, match="协议样本"):
            create_search_rank_deboost_task(session, 1, payload, operator="tester")
        # 任务不应被创建
        assert session.query(Task).filter_by(type="search_rank_deboost").count() == 0


# ==================== 3. 真实执行（mock Gateway）→ 写 SearchRankDeboostActionStat ====================


def test_e2e_build_plan_and_execute_with_gateway_fact_writes_stats() -> None:
    """完整链路仅按 Gateway 的实际点击事实写入统计。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session, account_ids=[100])
        # 直接构造 task（绕过 service 创建，复用已存在 binding）
        task = Task(
            id=str(uuid4()), tenant_id=1, name="E2E执行任务", type="search_rank_deboost",
            status="running", priority=3, timezone="Asia/Shanghai",
            account_config={}, pacing_config={}, failure_policy={},
            type_config={
                "search_bots": ["jisou"],
                "keywords": [{"text": "关键词A"}],
                "target_group_ids": [1001],
                "account_pool_id": 10,
                "proxy_airport_node_id": 20,
                "notes": "",
            },
            stats={}, next_run_at=_now(),
        )
        session.add(task)
        # 写入豁免群（非占位，避免触发 missing 告警）
        session.add(SearchRankDeboostExemptGroup(
            id=str(uuid4()), tenant_id=1, task_id=task.id,
            exempt_group_username="exempt_group", exempt_group_peer_id="-100999",
            exempt_group_title="豁免群", exempt_group_match_strategy="username",
            selected_at=_now(), selected_by="tester",
        ))
        session.commit()

        # build_plan 创建 action
        created = build_plan(session, task)
        assert created == 1
        action = session.query(Action).filter_by(task_id=task.id).one()
        assert action.action_type == "search_rank_deboost"
        assert action.status == "pending"
        payload_data = action.payload
        assert payload_data["bot_username"] == "jisou"
        assert payload_data["keyword_text_ciphertext"] != "关键词A"
        assert decrypt_secret(payload_data["keyword_text_ciphertext"]) == "关键词A"
        assert payload_data["runtime_environment"]["group_proxy_binding_id"] == str(binding.id)

        observed_keyword_texts: list[str] = []

        def gateway_execute(_account_id, _payload_data, **kwargs):
            keyword_text = kwargs["keyword_text"]
            observed_keyword_texts.append(keyword_text)
            return {
                "success": True,
                "execution_status": "confirmed",
                "observed_exit_ip": "1.1.1.1",
                "click_outcomes": [{
                    "status": "confirmed",
                    "competitor_username": "competitor_1",
                    "competitor_peer_id": "-10001",
                    "competitor_title": "竞争群 1",
                    "competitor_position": 1,
                    "row": 0,
                    "col": 0,
                    "text": "详情",
                    "url": "https://t.me/competitor_1",
                    "effect": "navigate_only",
                    "dwell_seconds": 12,
                    "joined": False,
                }],
            }

        from app.services.task_center.payloads import SearchRankDeboostPayload
        payload = SearchRankDeboostPayload.model_validate(action.payload)
        account = accounts[0]
        action.status = "executing"
        result = execute_search_rank_deboost(
            session, action, account, payload,
            gateway_execute=gateway_execute, probe_exit_ip="1.1.1.1",
        )

        assert result["success"] is True
        assert observed_keyword_texts == ["关键词A"]
        # SearchRankDeboostActionStat 已写入
        stats = session.query(SearchRankDeboostActionStat).filter_by(action_id=action.id).all()
        assert len(stats) == 1
        stat = stats[0]
        assert stat.skip_reason == ""
        assert stat.button_effect == "navigate_only"
        assert stat.joined is False
        assert stat.join_button_violation is False
        assert stat.dwell_seconds == 12
        assert stat.account_pool_id == 10
        assert stat.proxy_airport_node_id == 20
        assert stat.bot_username == "jisou"


# ==================== 4. stats 写入：task.stats 含 hourly_execution ====================


def test_e2e_build_plan_writes_hourly_execution_stats_to_task() -> None:
    """build_plan 后 task.stats 含 search_rank_deboost_stats.hourly_execution。"""
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session, account_ids=[100])
        task = Task(
            id=str(uuid4()), tenant_id=1, name="stats任务", type="search_rank_deboost",
            status="running", priority=3, timezone="Asia/Shanghai",
            account_config={}, pacing_config={}, failure_policy={},
            type_config={
                "search_bots": ["jisou"],
                "keywords": [{"text": "关键词A"}],
                "target_group_ids": [1001],
                "account_pool_id": 10,
                "proxy_airport_node_id": 20,
                "notes": "",
                "max_actions_per_hour": 5,
            },
            stats={}, next_run_at=_now(),
        )
        session.add(task)
        session.add(SearchRankDeboostExemptGroup(
            id=str(uuid4()), tenant_id=1, task_id=task.id,
            exempt_group_username="exempt_group", exempt_group_peer_id="-100999",
            exempt_group_title="豁免群", exempt_group_match_strategy="username",
            selected_at=_now(), selected_by="tester",
        ))
        session.commit()

        build_plan(session, task)

        stats = task.stats or {}
        deboost_stats = stats.get("search_rank_deboost_stats") or {}
        hourly = deboost_stats.get("hourly_execution") or {}
        assert hourly.get("bucket")  # ISO 时间桶
        assert hourly.get("goal") == 5
        assert hourly.get("max_actions_per_hour") == 5
        assert hourly.get("last_planned_count") == 1
        assert "capacity" in hourly
        assert "status" in hourly


# ==================== 5. 详情查询：get_task_detail 返回降权任务状态与 stats ====================


def test_e2e_get_task_detail_returns_deboost_task_with_stats() -> None:
    """get_task_detail 返回降权任务的 detail，含 task.type、stats、hourly_execution。"""
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session, account_ids=[100], with_binding=False)  # service 会自建 binding
        session.commit()

        payload = SearchRankDeboostTaskCreate(
            name="详情查询任务",
            search_bots=["jisou"],
            keywords=[{"text": "关键词A"}],
            target_group_ids=[1001],
            account_pool_id=10,
            proxy_airport_node_id=20,
            config={"max_actions_per_hour": 5},
        )
        task = create_search_rank_deboost_task(session, 1, payload, operator="tester")
        # service 已创建占位豁免群，更新为实际豁免群名以避免 missing 告警
        exempt = session.query(SearchRankDeboostExemptGroup).filter_by(task_id=task.id).one()
        exempt.exempt_group_username = "exempt_group"
        exempt.exempt_group_peer_id = "-100999"
        exempt.exempt_group_title = "豁免群"
        # 触发一次 build_plan 写入 hourly_execution
        task.status = "running"
        task.next_run_at = _now()
        session.commit()
        build_plan(session, task)
        session.commit()

        detail = get_task_detail(session, 1, task.id)

        assert detail["task"]["id"] == task.id
        assert detail["task"]["type"] == "search_rank_deboost"
        assert detail["task"]["status"] == "running"
        stats = detail["stats"]
        deboost_stats = stats.get("search_rank_deboost_stats") or {}
        hourly = deboost_stats.get("hourly_execution") or {}
        assert hourly.get("goal") == 5
        assert hourly.get("last_planned_count") == 1
        assert hourly.get("bucket")
