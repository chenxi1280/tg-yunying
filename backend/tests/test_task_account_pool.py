from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AccountRuntimeSummary, AccountStatus, Tenant, TgAccount, TgAccountSecuritySnapshot
from app.services.task_center.account_pool import select_task_accounts
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


def test_select_task_accounts_can_scan_beyond_concurrency_for_channel_capacity():
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


def test_membership_candidates_use_task_account_health_weighting():
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

    assert candidate_ids == [1, 3]
