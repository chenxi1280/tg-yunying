"""搜索排名观察任务审计记录测试（Task 20）。

覆盖：
- SubTask 20.1：创建/启动/暂停/重试/编辑配置/重选豁免群/分组级代理绑定变更 均写审计
- SubTask 20.2：审计摘要不含 Clash 订阅 URL、节点密码、token、关键词明文
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountProxy,
    AccountPool,
    AccountStatus,
    AuditLog,
    BotProtocolSample,
    OperationTarget,
    ProxyAirportNode,
    ProxyAirportSubscription,
    SearchRankDeboostExemptGroup,
    Tenant,
    TelegramDeveloperApp,
    TgAccount,
)
from app.schemas.task_center import (
    SearchRankDeboostTaskConfigUpdate,
    SearchRankDeboostTaskCreate,
)
from app.services.proxy_group_binding_service import (
    create_group_proxy_binding,
    failover_group_proxy_binding,
    unbind_group_proxy_binding,
)
from app.services.task_center.service import (
    create_search_rank_deboost_task,
    pause_task,
    reroll_search_rank_deboost_exempt_group,
    retry_task,
    start_task,
    update_search_rank_deboost_config,
)
from app.schemas.task_center import TaskRetryRequest
from app.security import encrypt_secret, encrypt_session


pytestmark = pytest.mark.no_postgres

# 敏感数据样本：审计 detail 不得包含这些值
SENSITIVE_KEYWORD_TEXT = "敏感关键词明文勿泄漏"
SENSITIVE_SUBSCRIPTION_URL = "https://clash.example.com/sub/secret-token-xyz"
SENSITIVE_TOKEN = "secret-token-xyz"
SENSITIVE_NODE_PASSWORD = "super-secret-node-password-123"


def _build_engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def _add_sample(session: Session, *, sample_type: str, structure_json: dict | None = None) -> None:
    session.add(
        BotProtocolSample(
            tenant_id=1,
            bot_username="jisou",
            sample_type=sample_type,
            sample_purpose="rank_deboost",
            is_active=True,
            structure_json=structure_json or {},
        )
    )


def _seed_sufficient_samples(session: Session) -> None:
    """插入达阈值的协议样本，使 validate_rank_deboost_preconditions 通过。"""
    for _ in range(2):
        _add_sample(session, sample_type="start_response")
    for _ in range(5):
        _add_sample(session, sample_type="search_results")
    for _ in range(3):
        _add_sample(session, sample_type="pagination_response")
    for effect in ("navigate_only", "join_candidate", "external_http_url"):
        _add_sample(session, sample_type="button_structure", structure_json={"button_effect": effect})
    for _ in range(3):
        _add_sample(session, sample_type="exit_ip_observation")


def _seed_base(session: Session) -> None:
    """种子数据：含敏感字段的订阅和节点，用于脱敏校验。"""
    session.add(Tenant(id=1, name="默认运营空间"))
    session.add(
        ProxyAirportSubscription(
            id=1,
            tenant_id=1,
            name="主订阅",
            enabled=True,
            sync_status="synced",
            healthy_node_count=3,
            subscription_url_ciphertext=SENSITIVE_SUBSCRIPTION_URL,
            subscription_url_preview="https://clash.example.com/sub/**",
        )
    )
    session.add(AccountPool(id=10, tenant_id=1, name="降权分组", pool_purpose="rank_deboost"))
    session.add(OperationTarget(
        id=1001,
        tenant_id=1,
        target_type="group",
        tg_peer_id="-1001",
        title="我方目标群",
        username="my_target",
    ))
    session.add(AccountProxy(
        id=30,
        tenant_id=1,
        name="rank-runtime",
        protocol="socks5",
        host="127.0.0.1",
        port=1080,
        status="healthy",
        alert_status="normal",
    ))
    session.add(TelegramDeveloperApp(
        id=40,
        app_name="rank-app",
        api_id=12345,
        api_hash_ciphertext=encrypt_secret("rank-api-hash"),
    ))
    session.add(TgAccount(
        id=100,
        tenant_id=1,
        pool_id=10,
        display_name="降权账号100",
        phone_masked="100",
        status=AccountStatus.ACTIVE.value,
        account_identity="rank_deboost",
        health_score=95,
        developer_app_id=40,
        developer_app_version=1,
        session_ciphertext=encrypt_session("rank-session-100"),
    ))
    session.add(
        ProxyAirportNode(
            id=20,
            tenant_id=1,
            subscription_id=1,
            node_key="node-20",
            status="healthy",
            observed_exit_ip="1.1.1.1",
            protocol="socks5",
            proxy_host="127.0.0.1",
            proxy_port=1080,
            node_config_ciphertext=SENSITIVE_NODE_PASSWORD,
        )
    )
    session.add(
        ProxyAirportNode(
            id=21,
            tenant_id=1,
            subscription_id=1,
            node_key="node-21",
            status="healthy",
            observed_exit_ip="2.2.2.2",
            protocol="socks5",
            proxy_host="127.0.0.2",
            proxy_port=1081,
        )
    )
    _seed_sufficient_samples(session)
    session.commit()


def _build_payload(**overrides) -> SearchRankDeboostTaskCreate:
    defaults = dict(
        name="降权任务",
        search_bots=["jisou"],
        keywords=[{"text": SENSITIVE_KEYWORD_TEXT}],
        target_group_ids=[1001],
        account_pool_id=10,
        proxy_airport_node_id=20,
        config={"per_account_daily_click_limit": 5},
        notes="",
    )
    defaults.update(overrides)
    return SearchRankDeboostTaskCreate(**defaults)


def _audit_logs(session: Session) -> list[AuditLog]:
    return list(session.query(AuditLog).order_by(AuditLog.id).all())


def _assert_no_sensitive(details: list[str]) -> None:
    """断言所有审计 detail 均不含敏感信息。"""
    for detail in details:
        assert SENSITIVE_KEYWORD_TEXT not in detail, f"审计 detail 含关键词明文: {detail!r}"
        assert SENSITIVE_SUBSCRIPTION_URL not in detail, f"审计 detail 含订阅 URL: {detail!r}"
        assert SENSITIVE_TOKEN not in detail, f"审计 detail 含 token: {detail!r}"
        assert SENSITIVE_NODE_PASSWORD not in detail, f"审计 detail 含节点密码: {detail!r}"


def _create_ready_task(session: Session, monkeypatch, actor: str = "alice"):
    task = create_search_rank_deboost_task(session, 1, _build_payload(), actor)
    exempt = session.query(SearchRankDeboostExemptGroup).filter_by(task_id=task.id).one()
    exempt.exempt_group_username = "real_exempt_group"
    exempt.exempt_group_peer_id = "-100999"
    exempt.exempt_group_title = "真实豁免群"
    session.commit()

    from app.services import _common

    monkeypatch.setattr(
        _common.gateway,
        "execute_search_rank_deboost",
        lambda *_args, **_kwargs: {"success": True, "search_results": [], "observed_exit_ip": "1.1.1.1"},
    )
    monkeypatch.setattr(_common.gateway, "supports_rank_deboost_observation", True)
    return start_task(session, 1, task.id, actor)


def _install_exempt_candidate_searcher(monkeypatch, usernames: list[str]) -> None:
    from app.services import _common

    remaining = iter(usernames)

    def search_rank_deboost_candidates(*_args, **_kwargs):
        username = next(remaining)
        return {
            "success": True,
            "execution_status": "candidates_found",
            "search_results": [{"username": username, "peer_id": f"-100{username}", "title": f"豁免群 {username}"}],
        }

    monkeypatch.setattr(
        _common.gateway,
        "search_rank_deboost_candidates",
        search_rank_deboost_candidates,
    )


# --- SubTask 20.1: 审计写入 ---


def test_create_task_writes_audit() -> None:
    """创建降权任务写审计。"""
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session)
        task = create_search_rank_deboost_task(session, 1, _build_payload(), "alice")

        logs = _audit_logs(session)
        create_logs = [l for l in logs if l.action == "创建任务中心任务"]
        assert len(create_logs) == 1
        log = create_logs[0]
        assert log.target_type == "task"
        assert log.target_id == task.id
        assert log.actor == "alice"
        assert log.tenant_id == 1
        assert log.created_at is not None


def test_start_ready_task_writes_audit(monkeypatch) -> None:
    """创建并启动降权任务写两条审计（创建 + 启动）。"""
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session)
        task = _create_ready_task(session, monkeypatch, actor="bob")

        logs = _audit_logs(session)
        actions = [l.action for l in logs]
        assert "创建任务中心任务" in actions
        assert "启动任务中心任务" in actions
        start_log = next(l for l in logs if l.action == "启动任务中心任务")
        assert start_log.target_id == task.id
        assert start_log.actor == "bob"


def test_pause_task_writes_audit(monkeypatch) -> None:
    """暂停降权任务写审计。"""
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session)
        task = _create_ready_task(session, monkeypatch)
        pause_task(session, 1, task.id, "alice")

        logs = _audit_logs(session)
        pause_logs = [l for l in logs if l.action == "暂停任务中心任务"]
        assert len(pause_logs) == 1
        assert pause_logs[0].target_id == task.id
        assert pause_logs[0].actor == "alice"


def test_retry_task_writes_audit(monkeypatch) -> None:
    """重试降权任务写审计。"""
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session)
        task = _create_ready_task(session, monkeypatch)
        retry_task(session, 1, task.id, TaskRetryRequest(failed_only=False), "alice")

        logs = _audit_logs(session)
        retry_logs = [l for l in logs if l.action == "重试任务中心任务"]
        assert len(retry_logs) == 1
        assert retry_logs[0].target_id == task.id
        assert retry_logs[0].actor == "alice"


def test_update_config_writes_audit() -> None:
    """编辑降权任务配置写审计。"""
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session)
        task = create_search_rank_deboost_task(session, 1, _build_payload(), "alice")
        update_search_rank_deboost_config(
            session,
            1,
            task.id,
            SearchRankDeboostTaskConfigUpdate(
                keywords=["审计配置变更关键词"],
            ),
            "carol",
        )

        logs = _audit_logs(session)
        update_logs = [l for l in logs if l.action == "更新任务类型配置"]
        assert len(update_logs) == 1
        assert update_logs[0].target_id == task.id
        assert update_logs[0].actor == "carol"


def test_reroll_writes_audit_with_old_new_operator_time(monkeypatch) -> None:
    """重选随机豁免群写审计，含旧豁免群、新豁免群、操作人、时间。"""
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session)
        task = create_search_rank_deboost_task(session, 1, _build_payload(), "alice")
        _install_exempt_candidate_searcher(monkeypatch, ["real_exempt_a", "real_exempt_b"])

        # 第一次重选：建立初始豁免群记录
        reroll_search_rank_deboost_exempt_group(session, 1, task.id, "alice")
        first_logs = [l for l in _audit_logs(session) if l.action == "重选搜索排名观察随机豁免群"]
        assert len(first_logs) == 1

        # 第二次重选：审计应含 previous_username（旧）和 new_username（新）
        reroll_search_rank_deboost_exempt_group(session, 1, task.id, "dave")
        reroll_logs = [l for l in _audit_logs(session) if l.action == "重选搜索排名观察随机豁免群"]
        assert len(reroll_logs) == 2
        second_log = reroll_logs[1]
        assert second_log.actor == "dave"
        assert second_log.target_id == task.id
        assert second_log.created_at is not None  # 时间
        detail = second_log.detail or ""
        assert "new_username=" in detail  # 新豁免群
        assert "previous_username=" in detail  # 旧豁免群


# --- SubTask 20.1: 分组级代理绑定变更审计 ---


def test_create_group_proxy_binding_writes_audit() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session)
        binding = create_group_proxy_binding(
            session,
            tenant_id=1,
            account_pool_id=10,
            proxy_airport_node_id=20,
            operator="alice",
        )
        logs = _audit_logs(session)
        create_logs = [l for l in logs if l.action == "create_group_proxy_binding"]
        assert len(create_logs) == 1
        assert create_logs[0].target_type == "account_group_proxy_binding"
        assert create_logs[0].target_id == str(binding.id)
        assert create_logs[0].actor == "alice"


def test_unbind_group_proxy_binding_writes_audit() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session)
        binding = create_group_proxy_binding(
            session, tenant_id=1, account_pool_id=10, proxy_airport_node_id=20, operator="alice"
        )
        unbind_group_proxy_binding(
            session, binding_id=binding.id, reason="manual_unbind", operator="bob"
        )
        logs = _audit_logs(session)
        unbind_logs = [l for l in logs if l.action == "unbind_group_proxy_binding"]
        assert len(unbind_logs) == 1
        assert unbind_logs[0].target_id == str(binding.id)
        assert unbind_logs[0].actor == "bob"
        assert "manual_unbind" in (unbind_logs[0].detail or "")


def test_failover_group_proxy_binding_writes_audit() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session)
        create_group_proxy_binding(
            session, tenant_id=1, account_pool_id=10, proxy_airport_node_id=20, operator="alice"
        )
        new_binding = failover_group_proxy_binding(
            session, tenant_id=1, account_pool_id=10, reason="node_degraded", operator="carol"
        )
        logs = _audit_logs(session)
        failover_logs = [l for l in logs if l.action == "failover_group_proxy_binding"]
        assert len(failover_logs) == 1
        assert failover_logs[0].target_id == str(new_binding.id)
        assert failover_logs[0].actor == "carol"
        detail = failover_logs[0].detail or ""
        assert "from_node=" in detail
        assert "to_node=" in detail


# --- SubTask 20.2: 审计摘要脱敏 ---


def test_audit_details_exclude_sensitive_info(monkeypatch) -> None:
    """执行全部降权任务操作后，所有审计 detail 不得含订阅 URL/节点密码/token/关键词明文。

    注：create_search_rank_deboost_task 内部已创建分组级代理绑定（pool 10 / node 20），
    因此分组级代理绑定变更通过 failover + unbind 覆盖（create 路径由任务创建覆盖，
    独立 create_group_proxy_binding 审计由专属测试覆盖）。
    """
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session)

        # 创建并启动（keywords 含敏感明文；任务创建内部绑定节点 20，其 config 含敏感密码）
        task = _create_ready_task(session, monkeypatch)
        # 暂停 + 重试
        pause_task(session, 1, task.id, "alice")
        retry_task(session, 1, task.id, TaskRetryRequest(failed_only=False), "alice")
        # 编辑配置（keywords 含敏感明文）
        update_search_rank_deboost_config(
            session,
            1,
            task.id,
            SearchRankDeboostTaskConfigUpdate(keywords=[SENSITIVE_KEYWORD_TEXT]),
            "alice",
        )
        # 重选豁免群
        _install_exempt_candidate_searcher(monkeypatch, ["real_exempt_a"])
        reroll_search_rank_deboost_exempt_group(session, 1, task.id, "alice")

        # 分组级代理绑定变更：任务已绑定 pool 10/node 20，failover 切换到 node 21
        new_binding = failover_group_proxy_binding(
            session, tenant_id=1, account_pool_id=10, reason="node_degraded", operator="alice"
        )
        # 解绑新绑定
        unbind_group_proxy_binding(
            session, binding_id=new_binding.id, reason="cleanup", operator="alice"
        )

        details = [l.detail or "" for l in _audit_logs(session)]
        assert details, "应至少产生一条审计记录"
        _assert_no_sensitive(details)


def test_audit_details_exclude_sensitive_info_for_binding_with_sensitive_node() -> None:
    """分组级代理绑定节点 20 的 node_config_ciphertext 含敏感密码，审计不得泄漏。"""
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session)
        create_group_proxy_binding(
            session, tenant_id=1, account_pool_id=10, proxy_airport_node_id=20, operator="alice"
        )
        failover_group_proxy_binding(
            session, tenant_id=1, account_pool_id=10, reason="node_degraded", operator="alice"
        )
        details = [l.detail or "" for l in _audit_logs(session) if l.action in (
            "create_group_proxy_binding",
            "failover_group_proxy_binding",
            "unbind_group_proxy_binding",
        )]
        _assert_no_sensitive(details)
