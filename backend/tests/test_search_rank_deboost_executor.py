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

from datetime import date, datetime, timedelta
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
    ExecutionAttempt,
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
from app.schemas.task_center import SearchRankDeboostTaskConfigUpdate, TaskRetryRequest
from app.security import encrypt_secret, encrypt_session
from app.services._common import _now
from app.services.task_center.dispatcher import dispatch_action
from app.services.task_center.executors.search_rank_deboost import (
    build_plan,
    execute_search_rank_deboost,
)
from app.services.task_center.executors import search_rank_deboost as rank_deboost_executor
from app.services.task_center.executors import search_rank_deboost_planner
from app.services.task_center import dispatcher as task_dispatcher
from app.services.task_center import service as task_service
from app.services.task_center import search_rank_deboost_pacing as rank_deboost_pacing
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
from app.services.task_center.search_rank_deboost_reservations import RESERVATION_TTL_MINUTES, reserve_click
from app.services.task_center.service import _mark_stale_executing_action, retry_task, update_search_rank_deboost_config
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
    session.add(OperationTarget(
        id=1001,
        tenant_id=1,
        target_type="group",
        tg_peer_id="-1001",
        title="我方目标群",
        username="my_target",
    ))
    session.add(ProxyAirportNode(id=proxy_node_id, tenant_id=1, subscription_id=1, node_key=f"node-{proxy_node_id}", status="healthy", observed_exit_ip=observed_exit_ip))
    session.add(AccountProxy(id=30, tenant_id=1, name="rank-runtime", protocol="socks5", host="127.0.0.1", port=1080, status="healthy", alert_status="normal"))
    session.add(TelegramDeveloperApp(id=40, app_name="rank-app", api_id=12345, api_hash_ciphertext=encrypt_secret("rank-api-hash")))
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
            developer_app_id=40,
            developer_app_version=1,
            session_ciphertext=encrypt_session(f"rank-session-{account_id}"),
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
    dwell_seconds_min: int = 10,
    dwell_seconds_max: int = 30,
) -> SearchRankDeboostPayload:
    return SearchRankDeboostPayload(
        bot_username="jisou",
        keyword_hash=keyword_hash,
        keyword_text_ciphertext=keyword_text,
        target_group_ids=target_group_ids or [1001],
        target_group_refs=[{"username": "my_target", "peer_id": "-1001"}],
        account_pool_id=account_pool_id,
        proxy_airport_node_id=proxy_node_id,
        exempt_group_username=exempt_group_username,
        dwell_seconds_min=dwell_seconds_min,
        dwell_seconds_max=dwell_seconds_max,
        runtime_environment={
            "proxy_egress_guard": "verified",
            "group_proxy_binding_id": str(binding_id),
            "runtime_proxy_id": "30",
            "binding_generation": "1",
            "proxy_airport_node_id": str(proxy_node_id),
            "account_pool_id": str(account_pool_id),
            "observed_exit_ip": observed_exit_ip,
        },
    )


def _gateway_result(
    status: str = "confirmed",
    *,
    observed_exit_ip: str = "1.1.1.1",
    effect: str = "navigate_only",
    dwell_seconds: int = 12,
    joined: bool = False,
) -> dict:
    result = {
        "success": status == "confirmed",
        "execution_status": status,
        "observed_exit_ip": observed_exit_ip,
        "click_outcomes": [],
    }
    if status in {"confirmed", "unknown_after_click"}:
        result["click_outcomes"] = [{
            "status": status,
            "competitor_username": "competitor_1",
            "competitor_peer_id": "-10001",
            "competitor_title": "竞争群 1",
            "competitor_position": 1,
            "row": 0,
            "col": 0,
            "text": "详情",
            "url": "https://t.me/competitor_1",
            "effect": effect,
            "dwell_seconds": dwell_seconds,
            "joined": joined,
        }]
    return result


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


def test_rank_planner_honors_task_daily_action_limit() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session, account_ids=[100, 101])
        task = _make_task(session, pacing_config={"max_actions_per_day": 1})
        session.query(SearchRankDeboostExemptGroup).update({SearchRankDeboostExemptGroup.task_id: task.id})
        session.commit()

        assert build_plan(session, task) == 1

        limits = task.stats["search_rank_deboost_stats"]["pacing_limits"]
        assert limits["task_daily_action_count"] == 1
        assert limits["task_daily_remaining"] == 0
        assert limits["task_daily_limit_reached"] == 1


def test_rank_planner_does_not_create_actions_during_quiet_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = datetime(2026, 7, 4, 3, 0, 0)
    monkeypatch.setattr(search_rank_deboost_planner, "_now", lambda: fixed_now)
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session, account_ids=[100])
        task = _make_task(session, pacing_config={"quiet_hours": {"start": "02:00", "end": "08:00"}})
        session.query(SearchRankDeboostExemptGroup).update({SearchRankDeboostExemptGroup.task_id: task.id})
        session.commit()

        assert build_plan(session, task) == 0

        assert session.query(Action).filter_by(task_id=task.id).count() == 0
        assert task.stats["search_rank_deboost_stats"]["pacing_limits"]["last_limit_reason"] == "quiet_hours_active"


def test_rank_planner_does_not_schedule_jittered_action_inside_quiet_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = datetime(2026, 7, 4, 1, 59, 0)
    monkeypatch.setattr(search_rank_deboost_planner, "_now", lambda: fixed_now)
    monkeypatch.setattr(
        search_rank_deboost_planner,
        "planned_action_at",
        lambda *_args, **_kwargs: fixed_now + timedelta(minutes=31),
    )
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session, account_ids=[100])
        task = _make_task(session, pacing_config={"quiet_hours": {"start": "02:00", "end": "08:00"}})
        session.query(SearchRankDeboostExemptGroup).update({SearchRankDeboostExemptGroup.task_id: task.id})
        session.commit()

        assert build_plan(session, task) == 0

        assert session.query(Action).filter_by(task_id=task.id).count() == 0
        assert task.stats["search_rank_deboost_stats"]["hourly_execution"]["last_blockers"] == {"quiet_hours_active": 1}


def test_rank_planner_applies_jitter_before_reserving_action(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = datetime(2026, 7, 4, 10, 0, 0)
    monkeypatch.setattr(search_rank_deboost_planner, "_now", lambda: fixed_now)
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session, account_ids=[100])
        task = _make_task(session, pacing_config={"daily_jitter_percent": 100, "hourly_jitter_percent": 100})
        task.id = "rank-jitter-task"
        session.query(SearchRankDeboostExemptGroup).update({SearchRankDeboostExemptGroup.task_id: task.id})
        session.commit()

        assert build_plan(session, task) == 1

        action = session.query(Action).filter_by(task_id=task.id).one()
        reservation = session.query(SearchRankDeboostClickReservation).filter_by(action_id=action.id).one()
        assert action.scheduled_at > fixed_now
        assert action.scheduled_at < datetime(2026, 7, 5, 0, 0, 0)
        assert reservation.expires_at >= action.scheduled_at + timedelta(minutes=RESERVATION_TTL_MINUTES)


def test_rank_daily_jitter_spreads_within_remaining_task_local_day(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = datetime(2026, 7, 4, 10, 0, 0)
    engine = _build_engine()
    with Session(engine) as session:
        task = _make_task(session, pacing_config={"daily_jitter_percent": 100, "hourly_jitter_percent": 0})
        task.id = "rank-daily-jitter"
        monkeypatch.setattr(rank_deboost_pacing, "_jitter_ratio", lambda *_args: 1.0)

        scheduled_at = rank_deboost_pacing.planned_action_at(task, "candidate", fixed_now)

        assert scheduled_at == datetime(2026, 7, 4, 23, 59, 59)


def test_rank_planner_jitter_never_schedules_after_task_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = datetime(2026, 7, 4, 10, 0, 0)
    deadline = fixed_now + timedelta(seconds=1)
    monkeypatch.setattr(search_rank_deboost_planner, "_now", lambda: fixed_now)
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session, account_ids=[100])
        task = _make_task(session, pacing_config={"daily_jitter_percent": 100, "hourly_jitter_percent": 100})
        task.scheduled_end = deadline
        session.query(SearchRankDeboostExemptGroup).update({SearchRankDeboostExemptGroup.task_id: task.id})
        session.commit()

        assert build_plan(session, task) == 1

        action = session.query(Action).filter_by(task_id=task.id).one()
        assert action.scheduled_at < deadline


def test_rank_operator_control_update_removes_superseded_pending_reservation() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        action.status = "pending"
        reservation = _make_reservation(session, task, action, account)
        reservation_id = reservation.id
        action_id = action.id
        session.commit()

        update_search_rank_deboost_config(
            session,
            1,
            task.id,
            SearchRankDeboostTaskConfigUpdate(max_actions_per_day=1),
            operator="tester",
        )

        assert session.get(Action, action_id) is None
        assert session.get(SearchRankDeboostClickReservation, reservation_id) is None


def test_rank_operator_control_update_only_preserves_executing_action_after_gateway_boundary() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session, account_ids=[100, 101])
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        claiming_action = _make_action(session, task, account, payload)
        claiming_action.status = "claiming"
        claiming_reservation = _make_reservation(session, task, claiming_action, account)
        pre_gateway_action = _make_action(session, task, account, payload)
        pre_gateway_reservation = _make_reservation(session, task, pre_gateway_action, account)
        session.add(
            ExecutionAttempt(
                tenant_id=1,
                action_id=pre_gateway_action.id,
                account_id=account.id,
                attempt_no=1,
                status="before_call",
                before_call_at=_now(),
                result_snapshot={},
            )
        )
        gateway_account = accounts[1]
        gateway_payload = _make_payload(task, account_id=gateway_account.id, binding_id=binding.id)
        gateway_action = _make_action(session, task, gateway_account, gateway_payload)
        gateway_reservation = _make_reservation(session, task, gateway_action, gateway_account)
        session.add(
            ExecutionAttempt(
                tenant_id=1,
                action_id=gateway_action.id,
                account_id=gateway_account.id,
                attempt_no=1,
                status="gateway_call_started",
                before_call_at=_now(),
                gateway_call_started_at=_now(),
                result_snapshot={},
            )
        )
        session.commit()

        update_search_rank_deboost_config(
            session,
            1,
            task.id,
            SearchRankDeboostTaskConfigUpdate(max_actions_per_day=1),
            operator="tester",
        )

        assert claiming_action.status == "skipped"
        assert claiming_reservation.status == "released"
        assert pre_gateway_action.status == "skipped"
        assert pre_gateway_reservation.status == "released"
        assert gateway_action.status == "executing"
        assert gateway_reservation.status == "reserved"


def test_rank_operator_control_update_supersedes_unmarked_executing_action() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        action.status = "executing"
        reservation = _make_reservation(session, task, action, account)
        session.commit()

        update_search_rank_deboost_config(
            session,
            1,
            task.id,
            SearchRankDeboostTaskConfigUpdate(max_actions_per_day=1),
            operator="tester",
        )

        assert action.status == "skipped"
        assert reservation.status == "released"


def test_start_expired_rank_draft_completes_before_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = datetime(2026, 7, 4, 10, 0, 0)
    monkeypatch.setattr(task_service, "_now", lambda: fixed_now)
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session)
        task = _make_task(session)
        task.status = "draft"
        task.scheduled_end = fixed_now - timedelta(seconds=1)
        session.commit()
        monkeypatch.setattr(
            task_service,
            "_prepare_rank_deboost_start",
            lambda *_args, **_kwargs: pytest.fail("过期任务不得执行 readiness"),
        )

        started = task_service.start_task(session, 1, task.id, "tester")

        assert started.status == "completed"
        assert started.next_run_at is None


def test_rank_display_and_control_update_replans_pending_actions() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(
            session,
            config={
                "target_operation_target_id": 1001,
                "target_reference_type": "operation_target",
                "target_title": "旧目标名称",
                "target_link": "https://t.me/my_target",
            },
        )
        task.stats = {"rank_deboost_readiness": {"status": "ready"}}
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        action.status = "pending"
        reservation = _make_reservation(session, task, action, account)
        action_id = action.id
        reservation_id = reservation.id
        session.commit()

        updated = update_search_rank_deboost_config(
            session,
            1,
            task.id,
            SearchRankDeboostTaskConfigUpdate(
                target_title="新目标名称",
                target_link="https://t.me/my_target",
                max_actions_per_day=1,
            ),
            operator="tester",
        )

        assert session.get(Action, action_id) is None
        assert session.get(SearchRankDeboostClickReservation, reservation_id) is None
        assert updated.status == "draft"
        assert updated.stats["rank_deboost_readiness"]["status"] == "ready"


def test_rank_pacing_update_keeps_ready_readiness_without_gateway_recheck(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session)
        task = _make_task(session)
        task.stats = {"rank_deboost_readiness": {"status": "ready"}}
        session.commit()

        updated = update_search_rank_deboost_config(
            session,
            1,
            task.id,
            SearchRankDeboostTaskConfigUpdate(max_actions_per_day=1),
            operator="tester",
        )
        assert updated.status == "draft"
        assert updated.stats["rank_deboost_readiness"]["status"] == "ready"
        monkeypatch.setattr(
            task_service,
            "_assert_rank_deboost_allows_start",
            lambda *_args, **_kwargs: pytest.fail("纯节奏修改不应重做 Gateway readiness"),
        )

        started = task_service.start_task(session, 1, updated.id, "tester")

        assert started.status == "running"


def test_rank_account_group_update_rechecks_binding_without_gateway_recheck(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session)
        session.add(AccountPool(id=11, tenant_id=1, name="未绑定黑搜索组", pool_purpose="rank_deboost"))
        task = _make_task(session)
        task.account_config = {"selection_mode": "group", "account_group_id": 10}
        task.stats = {"rank_deboost_readiness": {"status": "ready"}}
        session.commit()

        updated = update_search_rank_deboost_config(
            session,
            1,
            task.id,
            SearchRankDeboostTaskConfigUpdate(account_group_id=11),
            operator="tester",
        )
        monkeypatch.setattr(
            task_service,
            "_assert_rank_deboost_allows_start",
            lambda *_args, **_kwargs: pytest.fail("换黑账号组只应复验新分组绑定"),
        )

        with pytest.raises(ValueError, match="缺少 active runtime 代理绑定"):
            task_service.start_task(session, 1, updated.id, "tester")

        assert updated.stats["rank_deboost_readiness"]["status"] == "blocked"
        assert updated.stats["rank_deboost_readiness"]["required_check"] == "account_group_binding"


def test_rank_planner_caps_new_actions_by_target_count_and_unknown_slot() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session, account_ids=[100, 101, 102])
        task = _make_task(session, config={"target_count": 3})
        session.flush()
        session.add_all([
            Action(
                tenant_id=1,
                task_id=task.id,
                task_type=task.type,
                action_type="search_rank_deboost",
                account_id=100,
                status="success",
                payload={},
                result={
                    "execution_status": "confirmed",
                    "click_outcomes": [{
                        "status": "confirmed",
                        "competitor_username": "competitor",
                        "competitor_position": 1,
                        "row": 0,
                        "col": 0,
                        "dwell_seconds": 10,
                        "effect": "navigate_only",
                        "joined": False,
                    }],
                },
            ),
            Action(
                tenant_id=1,
                task_id=task.id,
                task_type=task.type,
                action_type="search_rank_deboost",
                account_id=100,
                status="unknown_after_send",
                payload={},
                result={},
            ),
        ])
        session.query(SearchRankDeboostExemptGroup).update({SearchRankDeboostExemptGroup.task_id: task.id})
        session.commit()

        assert build_plan(session, task) == 1
        assert session.query(Action).filter_by(task_id=task.id, action_type="search_rank_deboost").count() == 3
        assert task.stats["search_click_target"]["remaining_slot_count"] == 0


def test_build_plan_blocks_when_target_identity_is_unresolvable() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session)
        task = _make_task(session, target_group_ids=[9999])
        session.query(SearchRankDeboostExemptGroup).update({SearchRankDeboostExemptGroup.task_id: task.id})
        session.commit()

        created = build_plan(session, task)

        assert created == 0
        assert session.query(Action).filter_by(task_id=task.id).count() == 0
        assert "目标群缺少可验证" in (task.last_error or "")
        hourly = (task.stats or {}).get("search_rank_deboost_stats", {}).get("hourly_execution", {})
        assert hourly.get("block_code") == "target_identity_missing"


def test_build_plan_blocks_peer_only_target_identity() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session)
        task = _make_task(session)
        target = session.get(OperationTarget, 1001)
        target.username = ""
        session.query(SearchRankDeboostExemptGroup).update({SearchRankDeboostExemptGroup.task_id: task.id})
        session.commit()

        created = build_plan(session, task)

        assert created == 0
        assert session.query(Action).filter_by(task_id=task.id).count() == 0
        assert "可验证 username" in (task.last_error or "")


def test_build_plan_blocks_inverted_dwell_range() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        _seed_base(session)
        task = _make_task(session, config={"dwell_seconds_min": 30, "dwell_seconds_max": 10})
        session.query(SearchRankDeboostExemptGroup).update({SearchRankDeboostExemptGroup.task_id: task.id})
        session.commit()

        created = build_plan(session, task)

        assert created == 0
        assert session.query(Action).filter_by(task_id=task.id).count() == 0
        assert "dwell_seconds_max" in (task.last_error or "")


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

        gateway_execute = lambda *_args, **_kwargs: _gateway_result()

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

        gateway_execute = lambda *_args, **_kwargs: _gateway_result("unknown_after_click")

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

        gateway_execute = lambda *_args, **_kwargs: _gateway_result("observed_no_click")

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
            developer_app_id=40,
            developer_app_version=1,
            session_ciphertext=encrypt_session("rank-session-101"),
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


def _make_reservation(
    session: Session,
    task: Task,
    action: Action,
    account: TgAccount,
    *,
    status: str = "reserved",
) -> SearchRankDeboostClickReservation:
    now_value = _now()
    reservation = SearchRankDeboostClickReservation(
        tenant_id=task.tenant_id,
        task_id=task.id,
        action_id=action.id,
        account_id=account.id,
        account_pool_id=10,
        keyword_hash=KEYWORD_HASH_A,
        local_date=now_value.date(),
        hour_bucket=now_value.replace(minute=0, second=0, microsecond=0),
        reserved_count=1,
        consumed_count=1 if status in {"consumed", "unknown"} else 0,
        status=status,
        expires_at=now_value + timedelta(minutes=15),
    )
    session.add(reservation)
    session.flush()
    return reservation


def test_executor_fails_when_gateway_unavailable() -> None:
    """Gateway 不可用是可见失败，不能伪装成业务跳过。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        reservation = _make_reservation(session, task, action, account)
        session.commit()

        result = execute_search_rank_deboost(session, action, account, payload, gateway_execute=None, probe_exit_ip="1.1.1.1")

        assert result["success"] is False
        assert result["error_code"] == "rank_observation_gateway_unavailable"
        session.refresh(reservation)
        assert reservation.status == "released"
        assert reservation.consumed_count == 0


def test_retry_task_does_not_requeue_consumed_rank_reservation() -> None:
    """已确认点击的 rank_deboost action 不能被 retry_task 重置成 pending 后二次执行。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        action.status = "success"
        action.result = {"success": True}
        reservation = _make_reservation(session, task, action, account, status="consumed")
        session.commit()

        retry_task(session, 1, task.id, TaskRetryRequest(failed_only=False), "tester")

        session.refresh(action)
        session.refresh(reservation)
        assert action.status == "success"
        assert action.result["retry_skipped_reason"] == "rank_deboost_reservation_consumed"
        assert reservation.status == "consumed"


def test_retry_task_reopens_released_rank_reservation() -> None:
    """未点击释放的 rank_deboost action 可被 retry_task 显式重新预留。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        action.status = "skipped"
        action.result = {"success": False, "error_code": "rank_observation_gateway_unavailable"}
        reservation = _make_reservation(session, task, action, account, status="released")
        session.commit()

        retry_task(session, 1, task.id, TaskRetryRequest(failed_only=True), "tester")

        session.refresh(action)
        session.refresh(reservation)
        assert action.status == "pending"
        assert action.result == {}
        assert reservation.status == "reserved"
        assert reservation.consumed_count == 0


def test_retry_task_does_not_reopen_released_rank_reservation_after_quota_is_consumed() -> None:
    """released action 重试前必须重新检查同租户共享日配额。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session, config={"per_account_daily_click_limit": 1, "per_account_cooldown_hours": 0})
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        released_action = _make_action(session, task, account, payload)
        released_action.status = "skipped"
        released_reservation = _make_reservation(session, task, released_action, account, status="released")
        consumed_action = _make_action(session, task, account, payload)
        consumed_action.status = "success"
        _make_reservation(session, task, consumed_action, account, status="consumed")
        session.commit()

        retry_task(session, 1, task.id, TaskRetryRequest(failed_only=True), "tester")

        session.refresh(released_action)
        session.refresh(released_reservation)
        assert released_action.status == "skipped"
        assert released_reservation.status == "released"
        assert released_action.result["retry_skipped_reason"] == "rank_deboost_retry_quota_exhausted"


def test_retry_task_never_requeues_unknown_rank_action_without_reservation() -> None:
    """历史缺 reservation 的未知结果也不能被“重试全部”重新打开。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        action.status = "unknown_after_send"
        session.commit()

        retry_task(session, 1, task.id, TaskRetryRequest(failed_only=False), "tester")

        session.refresh(action)
        assert action.status == "unknown_after_send"
        assert action.result["retry_skipped_reason"] == "rank_deboost_gateway_outcome_unknown"


def test_executor_fails_when_proxy_egress_guard_failed() -> None:
    """分组级代理出口校验失败必须失败，不能静默跳过。"""
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
        assert result["error_code"] == "proxy_egress_guard_failed"


def test_executor_skips_when_target_not_in_results() -> None:
    """仅接受 Gateway 实测的 target_not_in_results，不在本地推断结果。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id, target_group_ids=[9999])
        action = _make_action(session, task, account, payload)
        _make_reservation(session, task, action, account)
        session.commit()

        gateway_execute = lambda *_args, **_kwargs: _gateway_result("target_not_in_results")

        result = execute_search_rank_deboost(session, action, account, payload, gateway_execute=gateway_execute, probe_exit_ip="1.1.1.1")

        assert result["success"] is False
        assert result["skip_reason"] == "target_not_in_results"


def test_executor_skips_when_all_exempt_clicks() -> None:
    """仅接受 Gateway 实测的 all_exempt_clicks，不根据结果列表本地合成。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        _make_reservation(session, task, action, account)
        session.commit()

        gateway_execute = lambda *_args, **_kwargs: _gateway_result("all_exempt_clicks")

        result = execute_search_rank_deboost(session, action, account, payload, gateway_execute=gateway_execute, probe_exit_ip="1.1.1.1")

        assert result["success"] is False
        assert result["skip_reason"] == "all_exempt_clicks"


def test_executor_writes_only_the_confirmed_gateway_click_fact() -> None:
    """confirmed 仅按 Gateway 返回的一条实际点击事实入库。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        _make_reservation(session, task, action, account)
        session.commit()

        gateway_execute = lambda *_args, **_kwargs: _gateway_result()

        result = execute_search_rank_deboost(session, action, account, payload, gateway_execute=gateway_execute, probe_exit_ip="1.1.1.1")

        assert result["success"] is True
        stats = session.query(SearchRankDeboostActionStat).filter_by(action_id=action.id).all()
        assert len(stats) == 1
        stat = stats[0]
        assert stat.skip_reason == ""
        assert stat.competitor_group_username == "competitor_1"
        assert stat.button_effect == "navigate_only"
        assert stat.joined is False
        assert stat.join_button_violation is False
        assert stat.dwell_seconds == 12
        assert stat.button_hash


def test_executor_skips_competitor_with_no_navigable_button() -> None:
    """没有与竞争结果精确绑定的按钮时 Gateway 必须报告 no_navigable_button。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        _make_reservation(session, task, action, account)
        session.commit()

        gateway_execute = lambda *_args, **_kwargs: _gateway_result("no_navigable_button")

        result = execute_search_rank_deboost(session, action, account, payload, gateway_execute=gateway_execute, probe_exit_ip="1.1.1.1")

        assert result["success"] is False
        assert result["skip_reason"] == "no_navigable_button"
        stats = session.query(SearchRankDeboostActionStat).filter_by(action_id=action.id).all()
        assert stats == []


def test_executor_accepts_factual_navigate_only_outcome() -> None:
    """Gateway 明确记录 navigate_only 且未加入时，执行器可确认该事实。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        _make_reservation(session, task, action, account)
        session.commit()

        gateway_execute = lambda *_args, **_kwargs: _gateway_result(effect="navigate_only", joined=False)

        result = execute_search_rank_deboost(session, action, account, payload, gateway_execute=gateway_execute, probe_exit_ip="1.1.1.1")

        assert result["success"] is True
        stats = session.query(SearchRankDeboostActionStat).filter_by(action_id=action.id).all()
        assert len(stats) == 1
        assert stats[0].button_effect == "navigate_only"
        assert stats[0].joined is False
        assert stats[0].join_button_detected is False
        assert stats[0].join_button_violation is False


def test_executor_rejects_confirmed_outcome_without_explicit_no_join_fact() -> None:
    """Gateway 已调用后缺少点击事实必须保留未知配额。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        reservation = _make_reservation(session, task, action, account)
        session.commit()

        gateway_result = _gateway_result()
        gateway_result["click_outcomes"][0].pop("joined")
        result = execute_search_rank_deboost(
            session,
            action,
            account,
            payload,
            gateway_execute=lambda *_args, **_kwargs: gateway_result,
            probe_exit_ip="1.1.1.1",
        )

        assert result["success"] is False
        assert result["error_code"] == "rank_deboost_gateway_contract_invalid"
        session.refresh(reservation)
        assert reservation.status == "unknown"
        assert session.query(SearchRankDeboostActionStat).filter_by(action_id=action.id).count() == 0


def test_executor_marks_reservation_unknown_for_unrecognized_gateway_status() -> None:
    """Gateway 已调用但未声明 no-click 时，未知状态不能释放配额。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        reservation = _make_reservation(session, task, action, account)
        session.commit()

        result = execute_search_rank_deboost(
            session,
            action,
            account,
            payload,
            gateway_execute=lambda *_args, **_kwargs: _gateway_result("unrecognized_status"),
            probe_exit_ip="1.1.1.1",
        )

        assert result["execution_status"] == "unknown_after_click"
        session.refresh(reservation)
        assert reservation.status == "unknown"


def test_executor_join_button_violation_directly() -> None:
    """Gateway 返回 join_candidate 实际事实时必须隔离账号并保留额度。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        _make_reservation(session, task, action, account)
        session.commit()

        gateway_execute = lambda *_args, **_kwargs: _gateway_result(effect="join_candidate")

        result = execute_search_rank_deboost(session, action, account, payload, gateway_execute=gateway_execute, probe_exit_ip="1.1.1.1")

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


def test_executor_stats_record_confirmed_gateway_fields() -> None:
    """统计字段必须来自 Gateway 的单条 confirmed outcome。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id, dwell_seconds_min=15, dwell_seconds_max=20)
        action = _make_action(session, task, account, payload)
        _make_reservation(session, task, action, account)
        session.commit()

        gateway_execute = lambda *_args, **_kwargs: _gateway_result(dwell_seconds=17)

        result = execute_search_rank_deboost(session, action, account, payload, gateway_execute=gateway_execute, probe_exit_ip="1.1.1.1")

        assert result["success"] is True
        stats = session.query(SearchRankDeboostActionStat).filter_by(action_id=action.id).all()
        assert len(stats) == 1
        stat = stats[0]
        assert stat.button_hash
        assert stat.competitor_position == 1
        assert stat.button_effect == "navigate_only"
        assert stat.dwell_seconds == 17
        assert stat.joined is False
        assert stat.join_button_detected is False
        assert stat.join_button_violation is False
        assert stat.account_pool_id == 10
        assert stat.proxy_airport_node_id == 20
        assert stat.bot_username == "jisou"
        assert stat.keyword_hash == KEYWORD_HASH_A


# ==================== Dispatcher 集成测试 ====================


def test_dispatch_action_fails_when_gateway_unavailable() -> None:
    """dispatch_action 必须把 Gateway 不可用暴露为失败。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        _make_reservation(session, task, action, account)
        session.commit()

        result = dispatch_action(session, action)

        assert result is True
        assert action.status == "failed"
        assert action.result["error_code"] == "rank_observation_gateway_unavailable"


def test_dispatch_action_fails_when_gateway_omits_egress_observation(monkeypatch) -> None:
    """Gateway 没有本次出口观察时，不能把绑定历史 IP 伪装成真实观察。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session, observed_exit_ip="1.1.1.1")
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        reservation = _make_reservation(session, task, action, account)
        session.commit()

        def fake_gateway(_account_id, _payload_data, **_kwargs):
            result = _gateway_result()
            result.pop("observed_exit_ip")
            return result

        from app.services import _common

        monkeypatch.setattr(_common.gateway, "execute_search_rank_deboost", fake_gateway)
        result = dispatch_action(session, action)

        assert result is True
        assert action.status == "unknown_after_send"
        assert action.result["error_code"] == "proxy_egress_guard_failed"
        assert reservation.status == "unknown"


def test_dispatch_action_uses_gateway_probe_instead_of_stored_binding_ip(monkeypatch) -> None:
    """Dispatcher 不得拿 binding.observed_exit_ip 伪装成本次出口探测。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session, observed_exit_ip="1.1.1.1")
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        reservation = _make_reservation(session, task, action, account)
        session.commit()

        def fake_gateway(_account_id, _payload_data, **_kwargs):
            return _gateway_result(observed_exit_ip="9.9.9.9")

        from app.services import _common

        monkeypatch.setattr(_common.gateway, "execute_search_rank_deboost", fake_gateway)

        result = dispatch_action(session, action)

        assert result is True
        assert action.status == "unknown_after_send"
        assert action.result["error_code"] == "proxy_egress_guard_failed"
        assert reservation.status == "unknown"
        assert session.query(SearchRankDeboostActionStat).filter_by(action_id=action.id).count() == 0


def test_dispatch_action_marks_invalid_gateway_result_unknown_and_nonretryable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gateway 返回非对象时，调用边界后的 action 不得释放后重试。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        reservation = _make_reservation(session, task, action, account)
        session.commit()

        from app.services import _common

        monkeypatch.setattr(_common.gateway, "execute_search_rank_deboost", lambda *_args, **_kwargs: [])

        assert dispatch_action(session, action) is True
        assert action.status == "unknown_after_send"
        assert action.result["error_code"] == "rank_deboost_gateway_contract_invalid"
        assert reservation.status == "unknown"

        retry_task(session, 1, task.id, TaskRetryRequest(failed_only=False), "tester")

        assert action.status == "unknown_after_send"
        assert action.result["retry_skipped_reason"] == "rank_deboost_gateway_outcome_unknown"


def test_dispatch_action_keeps_rank_reservation_unknown_when_gateway_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gateway 调用边界后的本地异常不能把一次可能已发生的点击重新开放。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        reservation = _make_reservation(session, task, action, account)
        session.commit()

        def raise_after_gateway(*_args, **_kwargs):
            raise RuntimeError("transport closed after gateway call")

        from app.services import _common

        monkeypatch.setattr(_common.gateway, "execute_search_rank_deboost", raise_after_gateway)

        result = dispatch_action(session, action)

        assert result is True
        assert action.status == "unknown_after_send"
        assert reservation.status == "unknown"


def test_dispatch_action_skips_expired_rank_reservation_before_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """失效预留的待执行 action 不能继续进入 Telegram 调用。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        reservation = _make_reservation(session, task, action, account)
        reservation.expires_at = _now() - timedelta(seconds=1)
        session.commit()

        from app.services import _common

        monkeypatch.setattr(
            _common.gateway,
            "execute_search_rank_deboost",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Gateway must not be called")),
        )

        result = dispatch_action(session, action)

        assert result is True
        assert action.status == "skipped"
        assert action.result["error_code"] == "rank_deboost_reservation_expired"
        assert reservation.status == "released"


def test_dispatch_action_skips_rank_after_task_deadline_before_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        task.scheduled_end = _now() - timedelta(seconds=1)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        reservation = _make_reservation(session, task, action, account)
        session.commit()

        from app.services import _common

        monkeypatch.setattr(
            _common.gateway,
            "execute_search_rank_deboost",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Gateway must not be called")),
        )

        assert dispatch_action(session, action) is True
        assert action.status == "skipped"
        assert action.result["error_code"] == "scheduled_end_reached"
        assert reservation.status == "released"
        assert task.status == "completed"


def test_dispatch_deadline_closes_other_unstarted_rank_reservations() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session, account_ids=[100, 101])
        task = _make_task(session)
        task.scheduled_end = _now() - timedelta(seconds=1)
        current_account, pending_account = accounts
        current_payload = _make_payload(task, account_id=current_account.id, binding_id=binding.id)
        current = _make_action(session, task, current_account, current_payload)
        current_reservation = _make_reservation(session, task, current, current_account)
        pending_payload = _make_payload(task, account_id=pending_account.id, binding_id=binding.id)
        pending = _make_action(session, task, pending_account, pending_payload)
        pending.status = "pending"
        pending_reservation = _make_reservation(session, task, pending, pending_account)
        claiming = _make_action(session, task, pending_account, pending_payload)
        claiming.status = "claiming"
        claiming_reservation = _make_reservation(session, task, claiming, pending_account)
        session.commit()

        assert dispatch_action(session, current) is True

        assert task.status == "completed"
        for action in (current, pending, claiming):
            assert action.status == "skipped"
            assert action.result["error_code"] == "scheduled_end_reached"
        for reservation in (current_reservation, pending_reservation, claiming_reservation):
            assert reservation.status == "released"


def test_planner_deadline_closes_pending_rank_reservation_before_replanning() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        task.scheduled_end = _now() - timedelta(seconds=1)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        action.status = "pending"
        reservation = _make_reservation(session, task, action, account)
        action_id = action.id
        reservation_id = reservation.id
        session.commit()

        assert task_service._check_stop_conditions(session, task) is True
        session.flush()

        assert task.status == "completed"
        assert session.get(Action, action_id) is None
        assert session.get(SearchRankDeboostClickReservation, reservation_id) is None


def test_rank_reservation_uses_scheduled_task_local_day() -> None:
    plan_now = datetime(2026, 7, 5, 11, 59, 0)
    scheduled_at = datetime(2026, 7, 5, 12, 0, 10)
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        task.timezone = "America/New_York"
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        action.scheduled_at = scheduled_at
        session.flush()

        reservation = reserve_click(
            session,
            task=task,
            action=action,
            account=account,
            account_pool_id=10,
            keyword_hash=KEYWORD_HASH_A,
            now_value=plan_now,
        )

        assert reservation.local_date == date(2026, 7, 5)
        assert reservation.hour_bucket.replace(tzinfo=None) == datetime(2026, 7, 5, 0, 0, 0)


def test_rank_planner_checks_daily_limit_for_scheduled_task_local_day(monkeypatch: pytest.MonkeyPatch) -> None:
    plan_now = datetime(2026, 7, 5, 11, 59, 0)
    scheduled_at = datetime(2026, 7, 5, 12, 0, 10)
    monkeypatch.setattr(search_rank_deboost_planner, "_now", lambda: plan_now)
    monkeypatch.setattr(search_rank_deboost_planner, "planned_action_at", lambda *_args, **_kwargs: scheduled_at)
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session, config={"max_actions_per_day": 1, "per_account_cooldown_hours": 0})
        task.timezone = "America/New_York"
        session.query(SearchRankDeboostExemptGroup).update({SearchRankDeboostExemptGroup.task_id: task.id})
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        reserved_action = _make_action(session, task, account, payload)
        reserved_action.scheduled_at = scheduled_at
        reserve_click(
            session,
            task=task,
            action=reserved_action,
            account=account,
            account_pool_id=10,
            keyword_hash=KEYWORD_HASH_A,
            now_value=plan_now,
        )
        session.commit()

        assert build_plan(session, task) == 0

        pacing_limits = task.stats["search_rank_deboost_stats"]["pacing_limits"]
        assert pacing_limits["last_limit_reason"] == "task_daily_limit_reached"


def test_dispatch_action_rechecks_rank_deadline_at_gateway_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = datetime(2026, 7, 4, 10, 0, 0)
    monkeypatch.setattr(task_dispatcher, "_now", lambda: fixed_now)
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        task.scheduled_end = fixed_now + timedelta(hours=1)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        reservation = _make_reservation(session, task, action, account)
        session.commit()

        def execute_after_deadline(_session, _action, _account, _payload, *, before_gateway_call):
            task.scheduled_end = fixed_now - timedelta(seconds=1)
            before_gateway_call()
            raise AssertionError("Gateway must not be called after deadline")

        monkeypatch.setattr(rank_deboost_executor, "execute_search_rank_deboost", execute_after_deadline)

        assert dispatch_action(session, action) is True
        assert action.status == "skipped"
        assert action.result["error_code"] == "scheduled_end_reached"
        assert reservation.status == "released"
        assert task.status == "completed"


def test_dispatch_action_skips_rank_during_quiet_hours_before_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = datetime(2026, 7, 4, 3, 0, 0)
    monkeypatch.setattr(task_dispatcher, "_now", lambda: fixed_now)
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session, pacing_config={"quiet_hours": {"start": "02:00", "end": "08:00"}})
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        reservation = _make_reservation(session, task, action, account)
        session.commit()

        from app.services import _common

        monkeypatch.setattr(
            _common.gateway,
            "execute_search_rank_deboost",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Gateway must not be called")),
        )

        assert dispatch_action(session, action) is True
        assert action.status == "skipped"
        assert action.result["error_code"] == "quiet_hours_active"
        assert reservation.status == "released"
        assert task.status == "running"


def test_dispatch_action_rechecks_rank_quiet_hours_at_gateway_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = datetime(2026, 7, 4, 3, 0, 0)
    monkeypatch.setattr(task_dispatcher, "_now", lambda: fixed_now)
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        reservation = _make_reservation(session, task, action, account)
        session.commit()

        def execute_inside_quiet_hours(_session, _action, _account, _payload, *, before_gateway_call):
            task.pacing_config = {"quiet_hours": {"start": "02:00", "end": "08:00"}}
            before_gateway_call()
            raise AssertionError("Gateway must not be called during quiet hours")

        monkeypatch.setattr(rank_deboost_executor, "execute_search_rank_deboost", execute_inside_quiet_hours)

        assert dispatch_action(session, action) is True
        assert action.status == "skipped"
        assert action.result["error_code"] == "quiet_hours_active"
        assert reservation.status == "released"


def test_dispatch_action_rechecks_rank_task_state_at_gateway_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        reservation = _make_reservation(session, task, action, account)
        session.commit()

        def pause_before_gateway(_session, _action, _account, _payload, *, before_gateway_call):
            task.status = "paused"
            session.flush()
            before_gateway_call()
            raise AssertionError("paused task must not call Gateway")

        monkeypatch.setattr(rank_deboost_executor, "execute_search_rank_deboost", pause_before_gateway)

        assert dispatch_action(session, action) is True
        assert action.status == "skipped"
        assert action.result["error_code"] == "task_not_active"
        assert reservation.status == "released"


def test_dispatch_action_releases_rank_reservation_when_account_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """账号预检失败时尚未调用 Gateway，预留不能继续占用日配额。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session, config=_retryable_rank_pacing_config())
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        reservation = _make_reservation(session, task, action, account)
        account.status = AccountStatus.LIMITED.value
        reservation.expires_at = _now() - timedelta(seconds=1)
        session.commit()

        from app.services import _common

        monkeypatch.setattr(
            _common.gateway,
            "execute_search_rank_deboost",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Gateway must not be called")),
        )

        assert dispatch_action(session, action) is True
        assert action.status == "failed"
        _assert_rank_reservation_released(session, task=task, account=account, reservation=reservation)


def test_dispatch_action_releases_rank_reservation_before_proxy_precheck() -> None:
    """代理绑定预检跳过时尚未调用 Gateway，预留必须释放。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session, config=_retryable_rank_pacing_config())
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        payload.runtime_environment["group_proxy_binding_id"] = ""
        action = _make_action(session, task, account, payload)
        reservation = _make_reservation(session, task, action, account)
        reservation.expires_at = _now() - timedelta(seconds=1)
        session.commit()

        assert dispatch_action(session, action) is True
        assert action.status == "skipped"
        assert action.result["error_code"] == "proxy_egress_guard_failed"
        _assert_rank_reservation_released(session, task=task, account=account, reservation=reservation)


def _retryable_rank_pacing_config() -> dict[str, int]:
    return {
        "per_account_daily_click_limit": 1,
        "per_account_cooldown_hours": 0,
        "max_actions_per_hour": 2,
    }


def _assert_rank_reservation_released(
    session: Session,
    *,
    task: Task,
    account: TgAccount,
    reservation: SearchRankDeboostClickReservation,
) -> None:
    assert reservation.status == "released"
    window = deboost_pacing_window(task, _now())
    stats = DeboostPacingStats(tenant_timezone="Asia/Shanghai", local_date=window.local_date.isoformat())
    assert account_click_allowed(session, task, account.id, KEYWORD_HASH_B, 10, window, stats) is True


def test_stale_rank_gateway_attempt_marks_reservation_unknown() -> None:
    """Worker 在 Gateway 边界后失联时，预留必须保持未知占用。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session)
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        reservation = _make_reservation(session, task, action, account)
        attempt = SimpleNamespace(
            gateway_call_started_at=_now(),
            status="gateway_call_started",
            after_call_at=None,
            result_snapshot={},
        )
        session.commit()

        _mark_stale_executing_action(
            action=action,
            task=task,
            latest_attempt=attempt,
            stale_worker_ids=set(),
            now=_now(),
        )

        assert action.status == "unknown_after_send"
        assert reservation.status == "unknown"


def test_stale_rank_before_gateway_releases_reservation() -> None:
    """Gateway 调用前超时的预留必须释放，后续规划可使用该配额。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(
            session,
            config={
                "per_account_daily_click_limit": 1,
                "per_account_cooldown_hours": 0,
                "max_actions_per_hour": 2,
            },
        )
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        reservation = _make_reservation(session, task, action, account)
        reservation.expires_at = _now() - timedelta(seconds=1)
        session.commit()

        attempt = SimpleNamespace(
            gateway_call_started_at=None,
            status="before_call",
            after_call_at=None,
            result_snapshot={},
        )
        _mark_stale_executing_action(
            action=action,
            task=task,
            latest_attempt=attempt,
            stale_worker_ids=set(),
            now=_now(),
        )

        assert action.status == "failed"
        assert reservation.status == "released"
        window = deboost_pacing_window(task, _now())
        stats = DeboostPacingStats(tenant_timezone="Asia/Shanghai", local_date=window.local_date.isoformat())
        assert account_click_allowed(session, task, account.id, KEYWORD_HASH_B, 10, window, stats) is True


# ==================== Pacing 限流测试 ====================


def test_planner_releases_expired_pending_reservation_before_daily_limit() -> None:
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session, config={"per_account_daily_click_limit": 1})
        account = accounts[0]
        session.query(SearchRankDeboostExemptGroup).update({SearchRankDeboostExemptGroup.task_id: task.id})
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        expired_action = _make_action(session, task, account, payload)
        expired_action.status = "pending"
        reservation = SearchRankDeboostClickReservation(
            tenant_id=1,
            task_id=task.id,
            action_id=expired_action.id,
            account_id=account.id,
            account_pool_id=10,
            keyword_hash=KEYWORD_HASH_A,
            local_date=_now().date(),
            hour_bucket=_now().replace(minute=0, second=0, microsecond=0),
            status="reserved",
            expires_at=_now() - timedelta(seconds=1),
        )
        session.add(reservation)
        session.commit()

        created = build_plan(session, task)

        assert created == 1
        assert expired_action.status == "skipped"
        assert reservation.status == "released"


def test_pacing_locks_tenant_before_reading_shared_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _build_engine()
    with Session(engine) as session:
        _binding, accounts = _seed_base(session)
        task = _make_task(session)
        session.commit()
        calls: list[tuple[int, str]] = []
        monkeypatch.setattr(
            rank_deboost_pacing,
            "lock_rank_deboost_quota_scope",
            lambda _session, locked_task: calls.append((locked_task.tenant_id, locked_task.id)),
            raising=False,
        )

        window = deboost_pacing_window(task, _now())
        stats = DeboostPacingStats(tenant_timezone="Asia/Shanghai", local_date=window.local_date.isoformat())
        account_click_allowed(session, task, accounts[0].id, KEYWORD_HASH_A, 10, window, stats)

        assert calls == [(1, task.id)]


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


def test_pacing_keeps_expired_executing_reservation_until_gateway_outcome_is_known() -> None:
    """执行中的预留过期不能被当成未调用 Gateway 而释放额度。"""
    engine = _build_engine()
    with Session(engine) as session:
        binding, accounts = _seed_base(session)
        task = _make_task(session, config={"per_account_daily_click_limit": 1, "per_account_cooldown_hours": 0})
        account = accounts[0]
        payload = _make_payload(task, account_id=account.id, binding_id=binding.id)
        action = _make_action(session, task, account, payload)
        reservation = _make_reservation(session, task, action, account)
        reservation.expires_at = _now() - timedelta(seconds=1)
        session.commit()

        window = deboost_pacing_window(task, _now())
        stats = DeboostPacingStats(tenant_timezone="Asia/Shanghai", local_date=window.local_date.isoformat())
        allowed = account_click_allowed(session, task, account.id, KEYWORD_HASH_B, 10, window, stats)

        assert allowed is False
        assert stats.last_limit_reason == "per_account_daily_click_limit_reached"


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
