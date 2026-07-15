from datetime import timedelta

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.database import Base
import pytest

from app.models import AccountPool, AccountRuntimeSummary, AccountStatus, Action, SchedulingSetting, Task, TaskAccountDailyCoverage, Tenant, TgAccount, TgAccountSecuritySnapshot, TgGroup, TgGroupAccount
from app.services._common import _now
from app.services.account_pools import (
    create_account_pool,
    ensure_code_receiver_account_pool,
    ensure_default_account_pool,
    move_account_pool,
    seed_account_pools,
    set_account_identity,
    update_account_pool,
)
from app.schemas import (
    AccountPoolCreate,
    AccountPoolOut,
    AccountPoolUpdate,
    DirectMessageTaskCreate,
    MessageSendTaskCreate,
)
from app.services.messages import create_message_send_task, create_pool_direct_message_task
from app.services.task_center.account_coverage import task_account_coverage
from app.services.task_center.account_pool import select_task_accounts
from app.services.task_center.channel_membership import candidate_accounts_for_config
from app.timezone import beijing_now

pytestmark = pytest.mark.no_postgres


def _add_normal_pool(session: Session, pool_id: int = 1000) -> AccountPool:
    pool = AccountPool(id=pool_id, tenant_id=1, name="普通账号组", pool_purpose="normal", is_default=True)
    session.add(pool)
    session.flush()
    return pool


def test_create_account_pool_persists_disabled_state_in_api_snapshot() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()
        result = create_account_pool(
            session,
            AccountPoolCreate(tenant_id=1, name="暂停使用", is_enabled=False),
            "creator",
        )
        output = AccountPoolOut.model_validate(result)
        saved = session.get(AccountPool, output.id)
        assert saved is not None
        assert output.is_enabled is saved.is_enabled is False
        assert output.is_default is saved.is_default is False
        assert output.disabled_at == saved.disabled_at
        assert output.disabled_at is not None
        assert output.disabled_by == saved.disabled_by == "creator"
        assert output.disable_reason == saved.disable_reason == ""


def test_update_account_pool_persists_disable_and_reenable_metadata() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        default_pool = AccountPool(id=1, tenant_id=1, name="默认池", is_default=True)
        pool = AccountPool(id=2, tenant_id=1, name="运营池")
        session.add_all([default_pool, pool])
        session.commit()
        disabled = update_account_pool(
            session,
            pool.id,
            AccountPoolUpdate(is_enabled=False, disable_reason="maintenance"),
            "operator",
        )
        assert disabled["is_enabled"] is False
        assert disabled["disabled_at"] is not None
        assert disabled["disabled_by"] == "operator"
        assert disabled["disable_reason"] == "maintenance"
        enabled = update_account_pool(session, pool.id, AccountPoolUpdate(is_enabled=True), "operator-2")
        assert enabled["is_enabled"] is True
        assert enabled["disabled_at"] is None
        assert enabled["disabled_by"] == ""
        assert enabled["disable_reason"] == ""


def test_update_account_pool_rejects_disabling_current_default_pool() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        pool = AccountPool(id=1, tenant_id=1, name="默认池", is_default=True)
        session.add(pool)
        session.commit()
        with pytest.raises(ValueError, match="default account pool must be enabled"):
            update_account_pool(session, pool.id, AccountPoolUpdate(is_enabled=False), "operator")
        assert pool.is_default is True
        assert pool.is_enabled is True


def test_update_account_pool_allows_unset_default_and_disable_in_same_patch() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        pool = AccountPool(id=1, tenant_id=1, name="默认池", is_default=True)
        session.add(pool)
        session.commit()
        result = update_account_pool(
            session,
            pool.id,
            AccountPoolUpdate(is_default=False, is_enabled=False, disable_reason="retired"),
            "operator",
        )
        assert result["is_default"] is False
        assert result["is_enabled"] is False
        assert result["disabled_at"] is not None
        assert result["disabled_by"] == "operator"
        assert result["disable_reason"] == "retired"


def test_update_account_pool_rejects_promoting_disabled_pool_to_default() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        pool = AccountPool(id=1, tenant_id=1, name="禁用池", is_enabled=False)
        session.add(pool)
        session.commit()
        with pytest.raises(ValueError, match="default account pool must be enabled"):
            update_account_pool(session, pool.id, AccountPoolUpdate(is_default=True), "operator")
        assert pool.is_default is False
        assert pool.is_enabled is False


def test_ensure_default_pool_skips_disabled_default_and_seed_uses_enabled_pool() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        disabled = AccountPool(id=1, tenant_id=1, name="历史禁用默认池", is_default=True, is_enabled=False)
        enabled = AccountPool(id=2, tenant_id=1, name="可用池", is_enabled=True)
        account = TgAccount(id=1, tenant_id=1, pool_id=None, display_name="待分配", phone_masked="1")
        session.add_all([disabled, enabled, account])
        session.commit()
        selected = ensure_default_account_pool(session, 1)
        assert selected.id == enabled.id
        assert selected.is_enabled is True
        assert selected.is_default is True
        assert disabled.is_default is False
        seed_account_pools(session)
        assert account.pool_id == enabled.id


def test_ensure_code_receiver_account_pool_creates_system_pool_once():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        first = ensure_code_receiver_account_pool(session, 1)
        second = ensure_code_receiver_account_pool(session, 1)

        assert first.id == second.id
        assert first.pool_purpose == "code_receiver"
        assert first.is_system is True
        assert first.system_key == "code_receiver"


def test_move_account_pool_syncs_code_receiver_identity():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        normal_pool = AccountPool(id=1, tenant_id=1, name="普通池", is_default=True)
        code_pool = AccountPool(id=2, tenant_id=1, name="接码池", pool_purpose="code_receiver", is_system=True, system_key="code_receiver")
        account = TgAccount(id=1, tenant_id=1, pool_id=1, display_name="账号", phone_masked="1", status=AccountStatus.ACTIVE.value)
        session.add_all([normal_pool, code_pool, account])
        session.commit()

        moved = move_account_pool(session, 1, 2, "tester")
        assert moved.account_identity == "code_receiver"

        moved_back = move_account_pool(session, 1, 1, "tester")
        assert moved_back.account_identity == "normal"


def test_set_account_identity_moves_account_between_code_receiver_and_default_pool():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        normal_pool = AccountPool(id=1, tenant_id=1, name="普通池", is_default=True)
        account = TgAccount(id=1, tenant_id=1, pool_id=1, display_name="账号", phone_masked="1", status=AccountStatus.ACTIVE.value)
        session.add_all([normal_pool, account])
        session.commit()

        code_receiver = set_account_identity(session, 1, "code_receiver", "tester")
        assert code_receiver.account_identity == "code_receiver"
        code_pool = session.get(AccountPool, code_receiver.pool_id)
        assert code_pool.pool_purpose == "code_receiver"

        normal = set_account_identity(session, 1, "normal", "tester")
        assert normal.account_identity == "normal"
        assert normal.pool_id == 1


def test_code_receiver_pool_account_cannot_create_direct_message_task():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        pool = AccountPool(id=2, tenant_id=1, name="接码池", pool_purpose="code_receiver", is_system=True, system_key="code_receiver")
        account = TgAccount(
            id=1,
            tenant_id=1,
            pool_id=2,
            display_name="接码账号",
            phone_masked="1",
            status=AccountStatus.ACTIVE.value,
            account_identity="code_receiver",
        )
        session.add_all([pool, account])
        session.commit()

        with pytest.raises(ValueError, match="接码专用账号不参与消息发送"):
            create_pool_direct_message_task(
                session,
                pool.id,
                DirectMessageTaskCreate(account_id=account.id, target_peer_id="@target", content="hi"),
                "tester",
            )


def test_code_receiver_account_cannot_create_message_send_task():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        account = TgAccount(
            id=1,
            tenant_id=1,
            display_name="接码账号",
            phone_masked="1",
            status=AccountStatus.ACTIVE.value,
            account_identity="code_receiver",
        )
        session.add(account)
        session.commit()

        with pytest.raises(ValueError, match="接码专用账号不参与消息发送"):
            create_message_send_task(
                session,
                MessageSendTaskCreate(account_id=account.id, target_type="private", target_peer_id="@target", content="hi"),
                "tester",
                tenant_id=1,
            )


def test_select_task_accounts_reduces_low_health_participation_weight():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        normal_pool = _add_normal_pool(session)
        for index in range(12):
            session.add(
                TgAccount(
                    id=index + 1,
                    tenant_id=1,
                    pool_id=normal_pool.id,
                    display_name=f"低分账号{index + 1}",
                    phone_masked=str(index + 1),
                    status=AccountStatus.ACTIVE.value,
                    account_identity="normal",
                    health_score=42,
                )
            )
        session.commit()

        selected = select_task_accounts(session, 1, {"max_concurrent": 12}, limit=12)

    assert len(selected) == 3


def test_select_task_accounts_ignores_concurrency_when_capacity_scan_requested():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        normal_pool = _add_normal_pool(session)
        for index in range(30):
            session.add(
                TgAccount(
                    id=index + 1,
                    tenant_id=1,
                    pool_id=normal_pool.id,
                    display_name=f"健康账号{index + 1}",
                    phone_masked=str(index + 1),
                    status=AccountStatus.ACTIVE.value,
                    account_identity="normal",
                    health_score=95,
                )
            )
        session.commit()

        selected = select_task_accounts(
            session,
            1,
            {"max_concurrent": 20},
            limit=30,
            enforce_max_concurrent=False,
        )

    assert len(selected) == 30


def test_select_task_accounts_prefers_healthy_accounts_before_low_health_accounts():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        normal_pool = _add_normal_pool(session)
        for account_id, score in [(1, 95), (2, 91), (3, 88), (4, 42), (5, 40), (6, 38), (7, 36)]:
            session.add(
                TgAccount(
                    id=account_id,
                    tenant_id=1,
                    pool_id=normal_pool.id,
                    display_name=f"账号{account_id}",
                    phone_masked=str(account_id),
                    status=AccountStatus.ACTIVE.value,
                    account_identity="normal",
                    health_score=score,
                )
            )
        session.commit()

        selected_ids = [account.id for account in select_task_accounts(session, 1, {"max_concurrent": 5}, limit=5)]

    assert selected_ids == [1, 2, 3, 4]


def test_select_task_accounts_uses_adjusted_health_score_from_security_snapshot():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        normal_pool = _add_normal_pool(session)
        session.add_all(
            [
                TgAccount(id=1, tenant_id=1, pool_id=normal_pool.id, display_name="健康账号", phone_masked="1", status=AccountStatus.ACTIVE.value, account_identity="normal", health_score=92),
                TgAccount(id=2, tenant_id=1, pool_id=normal_pool.id, display_name="安全阻塞账号", phone_masked="2", status=AccountStatus.ACTIVE.value, account_identity="normal", health_score=92),
            ]
        )
        session.add(
            TgAccountSecuritySnapshot(
                tenant_id=1,
                account_id=2,
                trusted_session_status="missing",
                two_fa_status="missing",
                profile_status="incomplete",
            )
        )
        session.commit()

        selected_ids = [account.id for account in select_task_accounts(session, 1, {"max_concurrent": 2}, limit=2)]

    assert selected_ids == [1]


def test_select_task_accounts_orders_by_runtime_health_score():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        normal_pool = _add_normal_pool(session)
        for account_id in range(1, 6):
            session.add(TgAccount(id=account_id, tenant_id=1, pool_id=normal_pool.id, display_name=f"基础高分{account_id}", phone_masked=str(account_id), status=AccountStatus.ACTIVE.value, account_identity="normal", health_score=95))
            session.add(AccountRuntimeSummary(tenant_id=1, account_id=account_id, health_score=20, risk_level="E"))
        session.add(TgAccount(id=6, tenant_id=1, pool_id=normal_pool.id, display_name="运行高分", phone_masked="6", status=AccountStatus.ACTIVE.value, account_identity="normal", health_score=10))
        session.add(AccountRuntimeSummary(tenant_id=1, account_id=6, health_score=92, risk_level="A"))
        session.commit()

        selected_ids = [account.id for account in select_task_accounts(session, 1, {"max_concurrent": 1}, limit=1)]

    assert selected_ids == [6]


def test_select_task_accounts_does_not_double_penalize_runtime_health_score():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        normal_pool = _add_normal_pool(session)
        session.add(
            TgAccount(
                id=1,
                tenant_id=1,
                pool_id=normal_pool.id,
                display_name="运行层可用账号",
                phone_masked="1",
                status=AccountStatus.ACTIVE.value,
                account_identity="normal",
                health_score=95,
            )
        )
        session.add(
            AccountRuntimeSummary(
                tenant_id=1,
                account_id=1,
                health_score=92,
                risk_level="A",
            )
        )
        session.add(
            TgAccountSecuritySnapshot(
                tenant_id=1,
                account_id=1,
                trusted_session_status="missing",
                two_fa_status="missing",
                profile_status="incomplete",
            )
        )
        session.commit()

        selected_ids = [
            account.id
            for account in select_task_accounts(session, 1, {"max_concurrent": 1}, limit=1)
        ]

    assert selected_ids == [1]


def test_select_task_accounts_filters_recent_successes_in_one_cooldown_window():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        normal_pool = _add_normal_pool(session)
        for account_id in range(1, 5):
            session.add(
                TgAccount(
                    id=account_id,
                    tenant_id=1,
                    pool_id=normal_pool.id,
                    display_name=f"账号{account_id}",
                    phone_masked=str(account_id),
                    status=AccountStatus.ACTIVE.value,
                    account_identity="normal",
                    health_score=95,
                )
            )
        session.add(Task(id="task-cooldown", tenant_id=1, name="冷却任务", type="group_ai_chat"))
        now_value = _now()
        session.add(
            Action(
                id="recent-success",
                tenant_id=1,
                task_id="task-cooldown",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=1,
                status="success",
                scheduled_at=now_value - timedelta(minutes=1),
                executed_at=now_value - timedelta(minutes=1),
            )
        )
        session.commit()

        selected_ids = [
            account.id
            for account in select_task_accounts(
                session,
                1,
                {"max_concurrent": 2, "cooldown_per_account_minutes": 5},
                limit=2,
            )
        ]

    assert selected_ids == [2, 3]


def test_select_task_accounts_prioritizes_uncovered_daily_task_accounts():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        normal_pool = _add_normal_pool(session)
        for account_id in range(1, 7):
            session.add(
                TgAccount(
                    id=account_id,
                    tenant_id=1,
                    pool_id=normal_pool.id,
                    display_name=f"账号{account_id}",
                    phone_masked=str(account_id),
                    status=AccountStatus.ACTIVE.value,
                    account_identity="normal",
                    health_score=95,
                )
            )
        session.add(Task(id="task-coverage", tenant_id=1, name="日内覆盖任务", type="group_ai_chat"))
        now_value = _now()
        for account_id in (1, 2):
            session.add(
                Action(
                    id=f"covered-{account_id}",
                    tenant_id=1,
                    task_id="task-coverage",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=account_id,
                    status="success",
                    scheduled_at=now_value,
                    executed_at=now_value,
                )
            )
        session.commit()

        selected_ids = [
            account.id
            for account in select_task_accounts(
                session,
                1,
                {"max_concurrent": 2},
                limit=2,
                daily_coverage_task_id="task-coverage",
                daily_coverage_action_types=("send_message",),
            )
        ]

    assert selected_ids == [3, 4]


def test_task_account_coverage_counts_same_day_unique_task_accounts():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        normal_pool = _add_normal_pool(session)
        for account_id in range(1, 7):
            session.add(
                TgAccount(
                    id=account_id,
                    tenant_id=1,
                    pool_id=normal_pool.id,
                    display_name=f"账号{account_id}",
                    phone_masked=str(account_id),
                    status=AccountStatus.ACTIVE.value,
                    account_identity="normal",
                    health_score=95,
                )
            )
        task = Task(
            id="task-coverage-stats",
            tenant_id=1,
            name="覆盖统计任务",
            type="group_ai_chat",
            account_config={"selection_mode": "manual", "account_ids": [1, 2, 3, 4, 5, 6], "max_concurrent": 2},
        )
        session.add(task)
        now_value = _now()
        session.add_all(
            [
                Action(
                    id="today-pending",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=1,
                    status="pending",
                    scheduled_at=now_value,
                ),
                Action(
                    id="today-success",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=2,
                    status="success",
                    executed_at=now_value,
                ),
                Action(
                    id="old-success",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=3,
                    status="success",
                    executed_at=now_value - timedelta(days=1),
                ),
                Action(
                    id="other-action-type",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="group_ai_chat",
                    action_type="view_message",
                    account_id=4,
                    status="success",
                    executed_at=now_value,
                ),
            ]
        )
        session.commit()

        coverage = task_account_coverage(session, task)

    assert coverage["covered_count"] == 2
    assert coverage["eligible_count"] == 6
    assert coverage["coverage_percent"] == 33
    assert coverage["action_types"] == ["send_message"]


def test_all_account_group_ai_task_uses_daily_success_coverage_when_legacy_config_is_natural():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="覆盖群", auth_status="已授权运营"))
        normal_pool = _add_normal_pool(session)
        for account_id in range(1, 4):
            session.add(
                TgAccount(
                    id=account_id,
                    tenant_id=1,
                    pool_id=normal_pool.id,
                    display_name=f"账号{account_id}",
                    phone_masked=str(account_id),
                    status=AccountStatus.ACTIVE.value,
                    account_identity="normal",
                    health_score=95,
                )
            )
            session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=account_id, can_send=True))
        task = Task(
            id="task-legacy-natural-all-accounts",
            tenant_id=1,
            name="旧配置全账号覆盖统计",
            type="group_ai_chat",
            account_config={"selection_mode": "all", "max_concurrent": 3},
            type_config={"target_group_id": 7, "account_coverage_mode": "natural"},
        )
        session.add(task)
        now_value = _now()
        session.add_all(
            [
                Action(
                    id="today-pending",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=1,
                    status="pending",
                    scheduled_at=now_value,
                ),
                Action(
                    id="today-success",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=2,
                    status="success",
                    executed_at=now_value,
                ),
            ]
        )
        session.commit()

        coverage = task_account_coverage(session, task)

    assert coverage["mode"] == "all_accounts_daily"
    assert coverage["statuses"] == ["confirmed"]
    assert coverage["coverage_status"] == "scope_uninitialized"
    assert coverage["covered_count"] == 0
    assert coverage["remaining_count"] == 0
    assert coverage["blocked_reasons"][0]["reason"] == "coverage_scope_uninitialized"


@pytest.mark.no_postgres
def test_group_ai_all_accounts_coverage_projects_reasons_and_pending_accounts():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="覆盖群", auth_status="已授权运营"))
        normal_pool = _add_normal_pool(session)
        for account_id in range(1, 5):
            session.add(
                TgAccount(
                    id=account_id,
                    tenant_id=1,
                    pool_id=normal_pool.id,
                    display_name=f"账号{account_id}",
                    phone_masked=str(account_id),
                    status=AccountStatus.ACTIVE.value,
                    account_identity="normal",
                    health_score=95,
                )
            )
        session.add_all(
            [
                TgGroupAccount(tenant_id=1, group_id=7, account_id=1, can_send=True),
                TgGroupAccount(tenant_id=1, group_id=7, account_id=2, can_send=False),
                TgGroupAccount(tenant_id=1, group_id=7, account_id=4, can_send=True),
            ]
        )
        task = Task(
            id="task-all-accounts-coverage-stats",
            tenant_id=1,
            name="全账号覆盖统计",
            type="group_ai_chat",
            account_config={"selection_mode": "all", "max_concurrent": 4},
            pacing_config={"max_actions_per_hour": 2},
            type_config={
                "target_group_id": 7,
                "account_coverage_mode": "all_accounts_daily",
                "per_account_daily_min_messages": 2,
                "per_account_daily_max_messages": 2,
                "coverage_window_hours": 24,
            },
            last_error="AI 候选不足",
        )
        session.add(task)
        session.add_all([
            TaskAccountDailyCoverage(
                tenant_id=1,
                task_id=task.id,
                group_id=7,
                account_id=1,
                coverage_date=beijing_now().date(),
                target_count=2,
                confirmed_count=1,
                state="ready",
            ),
            TaskAccountDailyCoverage(
                tenant_id=1,
                task_id=task.id,
                group_id=7,
                account_id=2,
                coverage_date=beijing_now().date(),
                target_count=2,
                state="blocked",
                blocker_code="cannot_send",
            ),
            TaskAccountDailyCoverage(
                tenant_id=1,
                task_id=task.id,
                group_id=7,
                account_id=3,
                coverage_date=beijing_now().date(),
                target_count=2,
                state="pending_admission",
            ),
            TaskAccountDailyCoverage(
                tenant_id=1,
                task_id=task.id,
                group_id=7,
                account_id=4,
                coverage_date=beijing_now().date(),
                target_count=2,
                confirmed_count=2,
                state="confirmed",
            ),
        ])
        session.add(
            Action(
                id="today-success-account-1",
                tenant_id=1,
                task_id=task.id,
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=1,
                status="success",
                executed_at=_now(),
            )
        )
        session.commit()

        coverage = task_account_coverage(session, task)

    assert coverage["mode"] == "all_accounts_daily"
    assert coverage["target_account_count"] == 4
    assert coverage["eligible_count"] == 4
    assert coverage["remaining_count"] == 3
    assert coverage["pending_admission_count"] == 1
    assert coverage["restricted_count"] == 1
    assert coverage["remaining_message_count"] == 5
    assert coverage["estimated_completion_window"]["estimated_min_hours"] == 3
    assert any(item["reason"] == "ready" for item in coverage["pending_accounts"])
    assert any(item["reason"] == "pending_admission" for item in coverage["pending_accounts"])
    assert any(item["reason"] == "cannot_send" for item in coverage["pending_accounts"])
    assert any(item["reason"] == "last_error" and item["message"] == "AI 候选不足" for item in coverage["blocked_reasons"])


def test_membership_candidates_include_all_active_config_accounts():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        normal_pool = _add_normal_pool(session)
        session.add_all(
            [
                TgAccount(id=1, tenant_id=1, pool_id=normal_pool.id, display_name="健康账号", phone_masked="1", status=AccountStatus.ACTIVE.value, account_identity="normal", health_score=92),
                TgAccount(id=2, tenant_id=1, pool_id=normal_pool.id, display_name="严重低分账号", phone_masked="2", status=AccountStatus.ACTIVE.value, account_identity="normal", health_score=20),
                TgAccount(id=3, tenant_id=1, pool_id=normal_pool.id, display_name="低分账号", phone_masked="3", status=AccountStatus.ACTIVE.value, account_identity="normal", health_score=42),
                TgAccount(id=4, tenant_id=1, pool_id=normal_pool.id, display_name="低分账号2", phone_masked="4", status=AccountStatus.ACTIVE.value, account_identity="normal", health_score=41),
            ]
        )
        session.commit()

        candidate_ids = [
            account.id
            for account in candidate_accounts_for_config(
                session,
                1,
                {"selection_mode": "manual", "account_ids": [1, 2, 3, 4], "max_concurrent": 4},
            )
        ]

    assert candidate_ids == [1, 2, 3, 4]


def test_membership_candidates_are_not_limited_by_send_concurrency():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        normal_pool = _add_normal_pool(session)
        for account_id in range(1, 31):
            session.add(
                TgAccount(
                    id=account_id,
                    tenant_id=1,
                    pool_id=normal_pool.id,
                    display_name=f"准入账号{account_id}",
                    phone_masked=str(account_id),
                    status=AccountStatus.ACTIVE.value,
                    account_identity="normal",
                    health_score=90,
                )
            )
        session.commit()

        candidate_ids = [
            account.id
            for account in candidate_accounts_for_config(
                session,
                1,
                {"selection_mode": "all", "max_concurrent": 20},
            )
        ]

    assert candidate_ids == list(range(1, 31))


def test_select_task_accounts_compares_capped_and_full_capacity_scan():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        normal_pool = _add_normal_pool(session)
        for account_id in range(1, 31):
            session.add(
                TgAccount(
                    id=account_id,
                    tenant_id=1,
                    pool_id=normal_pool.id,
                    display_name=f"频道账号{account_id}",
                    phone_masked=str(account_id),
                    status=AccountStatus.ACTIVE.value,
                    account_identity="normal",
                    health_score=90,
                )
            )
        session.commit()

        capped = select_task_accounts(session, 1, {"max_concurrent": 20}, limit=30)
        full_capacity = select_task_accounts(
            session,
            1,
            {"max_concurrent": 20},
            limit=30,
            enforce_max_concurrent=False,
        )

    assert len(capped) == 20
    assert len(full_capacity) == 30


def test_select_task_accounts_bulk_primes_capacity_for_full_pool_scan():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            SchedulingSetting(
                tenant_id=1,
                default_account_cooldown_seconds=120,
                default_account_hour_limit=10,
                default_account_day_limit=50,
            )
        )
        normal_pool = _add_normal_pool(session)
        session.add_all(
            TgAccount(
                id=account_id,
                tenant_id=1,
                pool_id=normal_pool.id,
                display_name=f"全量账号{account_id}",
                phone_masked=str(account_id),
                status=AccountStatus.ACTIVE.value,
                account_identity="normal",
                health_score=90,
            )
            for account_id in range(1, 121)
        )
        session.commit()
        select_count = 0

        def count_selects(_conn, _cursor, statement, _parameters, _context, _executemany):
            nonlocal select_count
            select_count += int(statement.lstrip().upper().startswith("SELECT"))

        event.listen(engine, "before_cursor_execute", count_selects)
        selected = select_task_accounts(
            session,
            1,
            {"max_concurrent": 120},
            limit=120,
            enforce_max_concurrent=False,
        )

    assert len(selected) == 120
    assert select_count <= 10
