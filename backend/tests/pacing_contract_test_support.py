from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Task, Tenant, TgAccount


def flat_curve() -> dict:
    return {
        "operation_profile": {
            "hourly_activity_curve": [1] * 24,
        },
    }


def pacing_engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="pacing tenant"))
        session.add(TgAccount(
            id=9101,
            tenant_id=1,
            display_name="pacing account",
            phone_masked="+86****9101",
        ))
        session.add(Task(id="pacing-task", tenant_id=1, name="pacing", type="channel_like"))
        session.commit()
    return engine
