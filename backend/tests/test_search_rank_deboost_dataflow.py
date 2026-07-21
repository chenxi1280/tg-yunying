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
    OperationTarget,
    ProxyAirportNode,
    Task,
    Tenant,
)
from app.models.search_rank_deboost import SearchRankDeboostExemptGroup
from app.permission_middleware import permission_check_result, required_permission
from app.schemas.task_center import (
    SearchRankDeboostExemptGroupResponse,
    SearchRankDeboostSimpleTaskCreate,
    SearchRankDeboostTaskConfigUpdate,
    SearchRankDeboostTaskCreate,
    TaskUpdate,
)
from app.services.task_center import service as task_service


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


def _simple_payload(**overrides) -> SearchRankDeboostSimpleTaskCreate:
    defaults = dict(
        target_title="我的目标群",
        target_link="https://t.me/my_target_group",
        keywords=["关键词"],
        target_count=8,
        account_group_id=10,
        max_actions_per_day=7,
        scheduled_end=datetime(2030, 1, 1, tzinfo=timezone.utc),
        daily_jitter_percent=20,
        hourly_jitter_percent=30,
    )
    defaults.update(overrides)
    return SearchRankDeboostSimpleTaskCreate(**defaults)


def _build_engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def test_simple_rank_create_maps_operator_controls_to_task_policy(monkeypatch) -> None:
    engine = _build_engine()
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(AccountPool(id=10, tenant_id=1, name="黑搜索执行组", pool_purpose="rank_deboost"))
        session.add(
            OperationTarget(
                id=1001,
                tenant_id=1,
                target_type="group",
                tg_peer_id="-1001001",
                title="我的目标群",
                username="my_target_group",
            )
        )
        session.commit()
        captured: dict = {}

        def fake_create(_session, _tenant_id, payload, _operator, *, commit=True, defer_readiness=False):
            captured["payload"] = payload
            captured["defer_readiness"] = defer_readiness
            return SimpleNamespace(id="simple-rank", name=payload.name)

        monkeypatch.setattr(task_service, "create_search_rank_deboost_task", fake_create)
        task = task_service.create_simple_search_rank_deboost_task(
            session,
            1,
            _simple_payload(keywords=["目标关键词"]),
            operator="tester",
        )

    payload = captured["payload"]
    assert task.name == "我的目标群 搜索排名观察 8 次"
    assert payload.target_group_ids == [1001]
    assert payload.account_config.selection_mode == "group"
    assert payload.account_config.account_group_id == 10
    assert payload.scheduled_end == datetime(2030, 1, 1, 8, 0, 0)
    assert payload.pacing_config.max_actions_per_day == 7
    assert payload.pacing_config.daily_jitter_percent == 20
    assert payload.pacing_config.hourly_jitter_percent == 30
    assert payload.config == {
        "target_count": 8,
        "target_operation_target_id": 1001,
        "target_reference_type": "operation_target",
        "target_title": "我的目标群",
        "target_link": "https://t.me/my_target_group",
    }
    assert captured["defer_readiness"] is True


def test_generic_search_click_patch_returns_bad_request_for_contract_error(monkeypatch) -> None:
    monkeypatch.setattr(
        router_module,
        "update_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("搜索点击任务必须通过专用编辑接口更新")),
    )

    with pytest.raises(HTTPException) as raised:
        router_module.patch_task("task-1", TaskUpdate(name="更新"), Session(), _make_user())

    assert raised.value.status_code == 400
    assert raised.value.detail == "搜索点击任务必须通过专用编辑接口更新"


def test_generic_patch_keeps_not_found_response(monkeypatch) -> None:
    monkeypatch.setattr(
        router_module,
        "update_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("task not found")),
    )

    with pytest.raises(HTTPException) as raised:
        router_module.patch_task("missing", TaskUpdate(name="更新"), Session(), _make_user())

    assert raised.value.status_code == 404


def test_simple_rank_create_rejects_system_managed_fields() -> None:
    with pytest.raises(Exception, match="Extra inputs are not permitted"):
        _simple_payload(account_config={"selection_mode": "manual", "account_ids": [1]})


# --- 成功路径：mock service 函数，验证路由委派 ---


def test_post_search_rank_deboost_task_creates_task(monkeypatch) -> None:
    captured: dict = {}

    def fake_create(session, tenant_id, payload, operator):
        captured["session"] = session
        captured["tenant_id"] = tenant_id
        captured["payload"] = payload
        captured["operator"] = operator
        return SimpleNamespace(id="task-1", name="系统生成名称", type="search_rank_deboost")

    monkeypatch.setattr(router_module, "create_simple_search_rank_deboost_task", fake_create)

    payload = _simple_payload()
    user = _make_user(tenant_id=1, name="alice")
    result = router_module.post_search_rank_deboost_task(
        payload=payload, session=object(), current_user=user
    )

    assert captured["tenant_id"] == 1
    assert captured["operator"] == "alice"
    assert captured["payload"] is payload
    assert result.name == "系统生成名称"
    assert result.type == "search_rank_deboost"


def test_post_task_start_returns_bad_request_for_rank_readiness_blocker(monkeypatch) -> None:
    def fail_start(*_args, **_kwargs):
        raise ValueError("搜索排名观察真实候选搜索缺少可执行黑账号")

    monkeypatch.setattr(router_module, "start_task", fail_start)

    with pytest.raises(HTTPException) as exc_info:
        router_module.post_task_start("rank-task", None, _make_user())

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "搜索排名观察真实候选搜索缺少可执行黑账号"


def test_post_search_rank_deboost_create_and_start_rejects_bypassing_draft() -> None:
    payload = _simple_payload()
    user = _make_user(tenant_id=7, name="bob")
    with pytest.raises(HTTPException, match="只能先创建草稿") as exc_info:
        router_module.post_search_rank_deboost_create_and_start(
            payload=payload, session=object(), current_user=user
        )

    assert exc_info.value.status_code == 400


def test_simple_rank_draft_creation_defers_readiness_to_start() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(AccountPool(id=10, tenant_id=1, name="黑搜索执行组", pool_purpose="rank_deboost"))
        session.add(
            OperationTarget(
                id=1001,
                tenant_id=1,
                target_type="group",
                tg_peer_id="-1001001",
                title="我的目标群",
                username="my_target_group",
            )
        )
        session.commit()

        task = task_service.create_simple_search_rank_deboost_task(
            session,
            1,
            _simple_payload(),
            operator="tester",
        )

        assert task.status == "draft"
        with pytest.raises(ValueError, match="协议样本不足"):
            task_service.start_task(session, 1, task.id, "tester")
        assert task.status == "draft"


def test_simple_rank_edit_regenerates_system_name() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(AccountPool(id=10, tenant_id=1, name="黑搜索执行组", pool_purpose="rank_deboost"))
        session.add_all([
            OperationTarget(
                id=1001,
                tenant_id=1,
                target_type="group",
                tg_peer_id="-1001001",
                title="我的目标群",
                username="my_target_group",
            ),
            OperationTarget(
                id=1002,
                tenant_id=1,
                target_type="group",
                tg_peer_id="-1001002",
                title="新的目标群",
                username="new_target_group",
            ),
        ])
        session.commit()
        task = task_service.create_simple_search_rank_deboost_task(
            session,
            1,
            _simple_payload(target_count=8),
            operator="tester",
        )

        target_updated = task_service.update_search_rank_deboost_config(
            session,
            1,
            task.id,
            SearchRankDeboostTaskConfigUpdate(
                target_title="新的目标群",
                target_link="https://t.me/new_target_group",
            ),
            operator="tester",
        )
        assert target_updated.name == "新的目标群 搜索排名观察 8 次"

        updated = task_service.update_search_rank_deboost_config(
            session,
            1,
            task.id,
            SearchRankDeboostTaskConfigUpdate(target_count=6),
            operator="tester",
        )

    assert updated.name == "新的目标群 搜索排名观察 6 次"


def test_simple_rank_edit_updates_operator_execution_controls() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all([
            AccountPool(id=10, tenant_id=1, name="黑搜索执行组一", pool_purpose="rank_deboost"),
            AccountPool(id=11, tenant_id=1, name="黑搜索执行组二", pool_purpose="rank_deboost"),
            OperationTarget(
                id=1001,
                tenant_id=1,
                target_type="group",
                tg_peer_id="-1001001",
                title="我的目标群",
                username="my_target_group",
            ),
        ])
        session.commit()
        task = task_service.create_simple_search_rank_deboost_task(session, 1, _simple_payload(), operator="tester")

        updated = task_service.update_search_rank_deboost_config(
            session,
            1,
            task.id,
            SearchRankDeboostTaskConfigUpdate(
                account_group_id=11,
                max_actions_per_day=4,
                scheduled_end=datetime(2030, 2, 1, tzinfo=timezone.utc),
                daily_jitter_percent=10,
                hourly_jitter_percent=15,
                quiet_hours={"start": "23:00", "end": "07:00"},
            ),
            operator="tester",
        )

    assert updated.account_config["selection_mode"] == "group"
    assert updated.account_config["account_group_id"] == 11
    assert updated.pacing_config["max_actions_per_day"] == 4
    assert updated.pacing_config["daily_jitter_percent"] == 10
    assert updated.pacing_config["hourly_jitter_percent"] == 15
    assert updated.pacing_config["quiet_hours"] == {"start": "23:00", "end": "07:00", "timezone": "Asia/Shanghai"}
    assert updated.scheduled_end == datetime(2030, 2, 1, 8, 0, 0)


def test_patch_search_rank_deboost_config(monkeypatch) -> None:
    captured: dict = {}

    def fake_update(session, tenant_id, task_id, payload, operator):
        captured.update(session=session, tenant_id=tenant_id, task_id=task_id, payload=payload, operator=operator)
        return SimpleNamespace(id=task_id, type="search_rank_deboost", status="running")

    monkeypatch.setattr(router_module, "update_search_rank_deboost_config", fake_update)

    payload = SearchRankDeboostTaskConfigUpdate(
        target_title="我的目标群",
        target_link="https://t.me/my_target_group",
        keywords=["更新关键词"],
        target_count=9,
    )
    user = _make_user(tenant_id=1, name="carol")
    result = router_module.patch_search_rank_deboost_config(
        task_id="task-3", payload=payload, session=object(), current_user=user
    )

    assert captured["task_id"] == "task-3"
    assert captured["operator"] == "carol"
    assert captured["payload"] is payload
    assert result.id == "task-3"


def test_rank_config_patch_rejects_system_managed_policy_fields() -> None:
    with pytest.raises(Exception, match="Extra inputs are not permitted"):
        SearchRankDeboostTaskConfigUpdate(config={"max_actions_per_hour": 999})


def test_rank_task_detail_includes_persisted_exempt_group() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        task = Task(
            tenant_id=1,
            name="黑搜索任务",
            type="search_rank_deboost",
            status="draft",
            type_config={},
            stats={},
        )
        session.add(task)
        session.flush()
        session.add(
            SearchRankDeboostExemptGroup(
                tenant_id=1,
                task_id=task.id,
                exempt_group_username="real_exempt",
                exempt_group_peer_id="-100123",
                exempt_group_title="真实豁免群",
                exempt_group_match_strategy="username",
                selected_by="tester",
            )
        )
        session.commit()

        detail = task_service.get_task_detail(session, 1, task.id)

    assert detail["rank_deboost_exempt_group"]["exempt_group_username"] == "real_exempt"


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


def test_legacy_rank_create_rejects_non_rank_deboost_pool() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(AccountPool(id=10, tenant_id=1, name="普通分组", pool_purpose="normal"))
        session.commit()

        payload = _build_payload(account_pool_id=10, proxy_airport_node_id=20)
        user = _make_user()

        with pytest.raises(ValueError, match="rank_deboost"):
            task_service.create_search_rank_deboost_task(session, 1, payload, user.name)


def test_legacy_rank_create_rejects_unhealthy_node() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(AccountPool(id=10, tenant_id=1, name="降权分组", pool_purpose="rank_deboost"))
        session.add(ProxyAirportNode(id=20, tenant_id=1, subscription_id=1, status="unhealthy"))
        session.commit()

        payload = _build_payload(account_pool_id=10, proxy_airport_node_id=20)
        user = _make_user()

        with pytest.raises(ValueError):
            task_service.create_search_rank_deboost_task(session, 1, payload, user.name)


def test_legacy_rank_create_rejects_node_used_by_authorization_slot() -> None:
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

        with pytest.raises(ValueError, match="授权槽位级绑定"):
            task_service.create_search_rank_deboost_task(session, 1, payload, user.name)


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
