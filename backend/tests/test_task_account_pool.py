from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AccountRuntimeSummary, AccountStatus, Action, Task, Tenant, TgAccount, TgAccountSecuritySnapshot
from app.services._common import _now
from app.services.task_center.account_pool import select_task_accounts, task_account_coverage
from app.services.task_center.channel_membership import candidate_accounts_for_config


def test_select_task_accounts_reduces_low_health_participation_weight():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        for index in range(12):
            session.add(
                TgAccount(
                    id=index + 1,
                    tenant_id=1,
                    display_name=f"低分账号{index + 1}",
                    phone_masked=str(index + 1),
                    status=AccountStatus.ACTIVE.value,
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
        for index in range(30):
            session.add(
                TgAccount(
                    id=index + 1,
                    tenant_id=1,
                    display_name=f"健康账号{index + 1}",
                    phone_masked=str(index + 1),
                    status=AccountStatus.ACTIVE.value,
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
        for account_id, score in [(1, 95), (2, 91), (3, 88), (4, 42), (5, 40), (6, 38), (7, 36)]:
            session.add(
                TgAccount(
                    id=account_id,
                    tenant_id=1,
                    display_name=f"账号{account_id}",
                    phone_masked=str(account_id),
                    status=AccountStatus.ACTIVE.value,
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
        session.add_all(
            [
                TgAccount(id=1, tenant_id=1, display_name="健康账号", phone_masked="1", status=AccountStatus.ACTIVE.value, health_score=92),
                TgAccount(id=2, tenant_id=1, display_name="安全阻塞账号", phone_masked="2", status=AccountStatus.ACTIVE.value, health_score=92),
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
        for account_id in range(1, 6):
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"基础高分{account_id}", phone_masked=str(account_id), status=AccountStatus.ACTIVE.value, health_score=95))
            session.add(AccountRuntimeSummary(tenant_id=1, account_id=account_id, health_score=20, risk_level="E"))
        session.add(TgAccount(id=6, tenant_id=1, display_name="运行高分", phone_masked="6", status=AccountStatus.ACTIVE.value, health_score=10))
        session.add(AccountRuntimeSummary(tenant_id=1, account_id=6, health_score=92, risk_level="A"))
        session.commit()

        selected_ids = [account.id for account in select_task_accounts(session, 1, {"max_concurrent": 1}, limit=1)]

    assert selected_ids == [6]


def test_select_task_accounts_does_not_double_penalize_runtime_health_score():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            TgAccount(
                id=1,
                tenant_id=1,
                display_name="运行层可用账号",
                phone_masked="1",
                status=AccountStatus.ACTIVE.value,
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
        for account_id in range(1, 5):
            session.add(
                TgAccount(
                    id=account_id,
                    tenant_id=1,
                    display_name=f"账号{account_id}",
                    phone_masked=str(account_id),
                    status=AccountStatus.ACTIVE.value,
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
        for account_id in range(1, 7):
            session.add(
                TgAccount(
                    id=account_id,
                    tenant_id=1,
                    display_name=f"账号{account_id}",
                    phone_masked=str(account_id),
                    status=AccountStatus.ACTIVE.value,
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
        for account_id in range(1, 7):
            session.add(
                TgAccount(
                    id=account_id,
                    tenant_id=1,
                    display_name=f"账号{account_id}",
                    phone_masked=str(account_id),
                    status=AccountStatus.ACTIVE.value,
                    health_score=95,
                )
            )
        task = Task(
            id="task-coverage-stats",
            tenant_id=1,
            name="覆盖统计任务",
            type="group_ai_chat",
            account_config={"selection_mode": "all", "max_concurrent": 2},
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


def test_membership_candidates_include_all_active_config_accounts():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(id=1, tenant_id=1, display_name="健康账号", phone_masked="1", status=AccountStatus.ACTIVE.value, health_score=92),
                TgAccount(id=2, tenant_id=1, display_name="严重低分账号", phone_masked="2", status=AccountStatus.ACTIVE.value, health_score=20),
                TgAccount(id=3, tenant_id=1, display_name="低分账号", phone_masked="3", status=AccountStatus.ACTIVE.value, health_score=42),
                TgAccount(id=4, tenant_id=1, display_name="低分账号2", phone_masked="4", status=AccountStatus.ACTIVE.value, health_score=41),
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
        for account_id in range(1, 31):
            session.add(
                TgAccount(
                    id=account_id,
                    tenant_id=1,
                    display_name=f"准入账号{account_id}",
                    phone_masked=str(account_id),
                    status=AccountStatus.ACTIVE.value,
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
        for account_id in range(1, 31):
            session.add(
                TgAccount(
                    id=account_id,
                    tenant_id=1,
                    display_name=f"频道账号{account_id}",
                    phone_masked=str(account_id),
                    status=AccountStatus.ACTIVE.value,
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
