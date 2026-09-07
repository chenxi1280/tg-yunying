import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import OperationTarget, TargetRuntimeSummary, Tenant
from app.services.runtime_summary import refresh_target_summary


@pytest.mark.no_postgres
def test_repeated_refresh_reuses_unflushed_target_summary():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as session:
        session.add(Tenant(id=1, name="summary identity"))
        session.add(OperationTarget(id=1, tenant_id=1, target_type="channel", tg_peer_id="-1001", title="channel"))
        session.commit()
        first = refresh_target_summary(session, 1, 1)
        second = refresh_target_summary(session, 1, 1)
        assert second is first
        session.commit()
        assert session.scalar(select(func.count(TargetRuntimeSummary.id))) == 1
