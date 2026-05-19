from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Tenant, TgAccount
from app.schemas import TenantUpdate
from app.services.tenants import ensure_account_quota_available, update_tenant


def _session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    return engine, Session()


def test_zero_account_quota_means_unlimited() -> None:
    engine, session = _session()
    try:
        tenant = Tenant(id=1, name="默认运营空间", account_quota=0, task_quota=5000)
        session.add(tenant)
        for index in range(50):
            session.add(TgAccount(tenant_id=1, display_name=f"账号{index}", phone_masked=f"+86138{index:08d}"))
        session.commit()

        ensure_account_quota_available(session, 1)

        update_tenant(session, 1, TenantUpdate(account_quota=0), "pytest")
        assert session.get(Tenant, 1).account_quota == 0
    finally:
        session.close()
        engine.dispose()


def test_legacy_positive_account_quota_no_longer_blocks_creation() -> None:
    engine, session = _session()
    try:
        tenant = Tenant(id=1, name="历史运营空间", account_quota=50, task_quota=5000)
        session.add(tenant)
        for index in range(50):
            session.add(TgAccount(tenant_id=1, display_name=f"历史账号{index}", phone_masked=f"+86139{index:08d}"))
        session.commit()

        ensure_account_quota_available(session, 1)
        update_tenant(session, 1, TenantUpdate(account_quota=10), "pytest")
        assert session.get(Tenant, 1).account_quota == 0
    finally:
        session.close()
        engine.dispose()
