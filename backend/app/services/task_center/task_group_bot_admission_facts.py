from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import AccountGroupAdmissionFact, TaskGroupBotAdmission
from app.services._common import _now

from .task_group_bot_admission_surface import fact_hash


def record_fact(
    session: Session,
    admission: TaskGroupBotAdmission,
    fact_kind: str,
    *,
    outcome: dict,
) -> None:
    identity = fact_hash({
        "kind": fact_kind,
        "outcome": outcome,
    })
    values = {
        "tenant_id": admission.tenant_id,
        "account_id": admission.account_id,
        "target_group_id": admission.target_group_id,
        "fact_kind": fact_kind,
        "fact_identity_hash": identity,
        "fact_version": int(admission.observation_version or 1),
        "outcome": outcome,
        "observed_at": _now(),
    }
    table = AccountGroupAdmissionFact.__table__
    insert = (
        pg_insert(table)
        if session.get_bind().dialect.name == "postgresql"
        else sqlite_insert(table)
    )
    session.execute(insert.values(**values).on_conflict_do_nothing(
        index_elements=[
            "account_id",
            "target_group_id",
            "fact_kind",
            "fact_identity_hash",
        ]
    ))


__all__ = ["record_fact"]
