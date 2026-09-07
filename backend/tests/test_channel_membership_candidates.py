import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AccountPool, AccountStatus, Tenant, TgAccount
from app.services.task_center.channel_membership import candidate_accounts_for_config

pytestmark = pytest.mark.no_postgres


@pytest.mark.parametrize("selection,expected", [
    ({"account_group_ids": [1, 2], "account_group_id": None}, {101, 102}),
    ({"account_group_ids": [1, 2], "account_group_id": 3}, {101, 102}),
    ({"account_group_id": 2}, {102}),
    ({"account_group_ids": [], "account_group_id": 2}, {102}),
    ({"account_group_ids": [], "account_group_id": None}, set()),
])
def test_membership_candidates_supports_account_group_ids(selection, expected):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        pool1 = AccountPool(id=1, tenant_id=1, name="分组1", pool_purpose="normal", is_default=True)
        pool2 = AccountPool(id=2, tenant_id=1, name="分组2", pool_purpose="normal", is_default=False)
        pool3 = AccountPool(id=3, tenant_id=1, name="分组3", pool_purpose="normal", is_default=False)
        session.add_all([pool1, pool2, pool3])
        session.add(TgAccount(id=101, tenant_id=1, pool_id=1, display_name="账号1", phone_masked="101", status=AccountStatus.ACTIVE.value, health_score=90))
        session.add(TgAccount(id=102, tenant_id=1, pool_id=2, display_name="账号2", phone_masked="102", status=AccountStatus.ACTIVE.value, health_score=90))
        session.add(TgAccount(id=103, tenant_id=1, pool_id=3, display_name="账号3", phone_masked="103", status=AccountStatus.ACTIVE.value, health_score=90))
        session.commit()

        cands = candidate_accounts_for_config(
            session,
            1,
            {"selection_mode": "group", **selection},
        )
        assert {a.id for a in cands} == expected
