from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.routers import task_center as router_module
from app.database import Base
from app.models import (
    AccountPool,
    AccountProxyBinding,
    ProxyAirportNode,
    Tenant,
)
from app.permission_middleware import permission_check_result, required_permission
from app.schemas.task_center import (
    SearchRankDeboostExemptGroupResponse,
    SearchRankDeboostTaskConfigUpdate,
    SearchRankDeboostTaskCreate,
)


pytestmark = pytest.mark.no_postgres


def _make_user(tenant_id: int = 1, name: str = "op") -> SimpleNamespace:
    return SimpleNamespace(tenant_id=tenant_id, name=name)


def _build_payload(**overrides) -> SearchRankDeboostTaskCreate:
    defaults = dict(
        name="降权任务",
        search_bots=["jisou"],
        keywords=[{"text": "关键词"}],
        target_group_ids=[1001],
        account_pool_id=10,
        proxy_airport_node_id=20,
        config={"per_account_daily_click_limit": 5},
        notes="",
    )
    defaults.update(overrides)
    return SearchRankDeboostTaskCreate(**defaults)


def _build_engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


# --- 成功路径：mock service 函数，验证路由委派 ---


def test_post_search_rank_deboost_task_creates_task(monkeypatch) -> None:
    captured: dict = {}

    def fake_create(session, tenant_id, payload, operator):
        captured["session"] = session
        captured["tenant_id"] = tenant_id
        captured["payload"] = payload
        captured["operator"] = operator
        return SimpleNamespace(id="task-1", name=payload.name, type="search_rank_deboost")

    monkeypatch.setattr(router_module, "create_search_rank_deboost_task", fake_create)

    payload = _build_payload()
    user = _make_user(tenant_id=1, name="alice")
    result = router_module.post_search_rank_deboost_task(
        payload=payload, session=object(), current_user=user
    )

    assert captured["tenant_id"] == 1
    assert captured["operator"] == "alice"
    assert captured["payload"] is payload
    assert result.name == "降权任务"
    assert result.type == "search_rank_deboost"


def test_post_search_rank_deboost_create_and_start(monkeypatch) -> None:
    captured: dict = {}

    def fake_create_and_start(session, tenant_id, payload, operator):
        captured.update(session=session, tenant_id=tenant_id, payload=payload, operator=operator)
        return SimpleNamespace(id="task-2", type="search_rank_deboost", status="running")

    monkeypatch.setattr(router_module, "create_and_start_search_rank_deboost_task", fake_create_and_start)

    payload = _build_payload(name="启动任务")
    user = _make_user(tenant_id=7, name="bob")
    result = router_module.post_search_rank_deboost_create_and_start(
        payload=payload, session=object(), current_user=user
    )

    assert captured["tenant_id"] == 7
    assert captured["operator"] == "bob"
    assert result.status == "running"


def test_patch_search_rank_deboost_config(monkeypatch) -> None:
    captured: dict = {}

    def fake_update(session, tenant_id, task_id, payload, operator):
        captured.update(session=session, tenant_id=tenant_id, task_id=task_id, payload=payload, operator=operator)
        return SimpleNamespace(id=task_id, type="search_rank_deboost", status="running")

    monkeypatch.setattr(router_module, "update_search_rank_deboost_config", fake_update)

    payload = SearchRankDeboostTaskConfigUpdate(notes="更新备注")
    user = _make_user(tenant_id=1, name="carol")
    result = router_module.patch_search_rank_deboost_config(
        task_id="task-3", payload=payload, session=object(), current_user=user
    )

    assert captured["task_id"] == "task-3"
    assert captured["operator"] == "carol"
    assert captured["payload"] is payload
    assert result.id == "task-3"


def test_post_search_rank_deboost_reroll_exempt_group(monkeypatch) -> None:
    captured: dict = {}
    expected = SearchRankDeboostExemptGroupResponse(
        task_id="task-4",
        exempt_group_username="exempt_group_x",
        exempt_group_peer_id="-100123",
        exempt_group_title="豁免群",
        exempt_group_match_strategy="username",
        selected_at=datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc),
        selected_by="dave",
    )

    def fake_reroll(session, tenant_id, task_id, operator):
        captured.update(session=session, tenant_id=tenant_id, task_id=task_id, operator=operator)
        return expected

    monkeypatch.setattr(router_module, "reroll_search_rank_deboost_exempt_group", fake_reroll)

    user = _make_user(tenant_id=1, name="dave")
    result = router_module.post_search_rank_deboost_reroll_exempt_group(
        task_id="task-4", session=object(), current_user=user
    )

    assert captured["task_id"] == "task-4"
    assert captured["operator"] == "dave"
    assert result is expected
    assert result.exempt_group_username == "exempt_group_x"


# --- 拒绝路径：真实 SQLite + 真实 service 校验，经路由触发 400 ---


def test_post_search_rank_deboost_task_rejects_non_rank_deboost_pool() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(AccountPool(id=10, tenant_id=1, name="普通分组", pool_purpose="normal"))
        session.commit()

        payload = _build_payload(account_pool_id=10, proxy_airport_node_id=20)
        user = _make_user()

        with pytest.raises(HTTPException) as exc_info:
            router_module.post_search_rank_deboost_task(
                payload=payload, session=session, current_user=user
            )
        assert exc_info.value.status_code == 400
        assert "rank_deboost" in exc_info.value.detail


def test_post_search_rank_deboost_task_rejects_unhealthy_node() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(AccountPool(id=10, tenant_id=1, name="降权分组", pool_purpose="rank_deboost"))
        session.add(ProxyAirportNode(id=20, tenant_id=1, subscription_id=1, status="unhealthy"))
        session.commit()

        payload = _build_payload(account_pool_id=10, proxy_airport_node_id=20)
        user = _make_user()

        with pytest.raises(HTTPException) as exc_info:
            router_module.post_search_rank_deboost_task(
                payload=payload, session=session, current_user=user
            )
        assert exc_info.value.status_code == 400


def test_post_search_rank_deboost_task_rejects_node_used_by_authorization_slot() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(AccountPool(id=10, tenant_id=1, name="降权分组", pool_purpose="rank_deboost"))
        session.add(ProxyAirportNode(id=20, tenant_id=1, subscription_id=1, status="healthy"))
        session.add(
            AccountProxyBinding(
                id=1,
                tenant_id=1,
                account_id=999,
                proxy_airport_node_id=20,
                status="active",
            )
        )
        session.commit()

        payload = _build_payload(account_pool_id=10, proxy_airport_node_id=20)
        user = _make_user()

        with pytest.raises(HTTPException) as exc_info:
            router_module.post_search_rank_deboost_task(
                payload=payload, session=session, current_user=user
            )
        assert exc_info.value.status_code == 400
        assert "授权槽位级绑定" in exc_info.value.detail


# --- 权限 403 闸门 ---


def test_post_search_rank_deboost_task_unauthorized_returns_403() -> None:
    """无权限用户调用降权任务路由应被权限中间件阻断（返回 403）。

    权限校验由 permission_middleware 在请求层处理（Task 11）。这里验证：
    1. 路由命中 tasks.create.search_rank_deboost 权限规则
    2. 缺少该权限的用户会被标记为 missing（→ 403）
    3. reroll 路由命中 tasks.manage.search_rank_deboost，无权限 → missing（→ 403）
    """
    permissions = required_permission("POST", "/api/tasks/search_rank_deboost")
    assert permissions is not None
    assert "tasks.create.search_rank_deboost" in permissions

    # 用户只有 tasks.manage 但缺少 tasks.create.search_rank_deboost → 阻断
    missing = permission_check_result(permissions, {"tasks.manage"})
    assert "tasks.create.search_rank_deboost" in missing

    # 用户两者都有 → 放行
    missing_full = permission_check_result(
        permissions, {"tasks.manage", "tasks.create.search_rank_deboost"}
    )
    assert missing_full == []

    # reroll 路由需要 tasks.manage.search_rank_deboost
    reroll_permissions = required_permission(
        "POST", "/api/tasks/123/search_rank_deboost_reroll_exempt_group"
    )
    assert reroll_permissions == ("tasks.manage.search_rank_deboost",)
    missing_reroll = permission_check_result(reroll_permissions, set())
    assert missing_reroll == ["tasks.manage.search_rank_deboost"]
