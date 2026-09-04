"""Idempotent initialization and explicit successors for retired runtime policies."""
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert


REVISION_FIELDS = frozenset({"id", "revision", "state", "effective_from", "effective_to", "created_at", "updated_at"})


def ensure_runtime_policy(session, model, *, scope, defaults):
    session.flush()
    query = select(model).filter_by(**scope)
    active = session.scalar(query.where(model.state == "active"))
    if active is not None:
        return active
    previous = session.scalar(query.order_by(model.revision.desc()).limit(1))
    if previous is not None and previous.state not in {"retired", "superseded"}:
        raise ValueError(f"runtime_policy_inactive:{model.__tablename__}:{previous.state}")
    values = dict(defaults) if previous is None else {
        column.key: getattr(previous, column.key)
        for column in model.__table__.columns if column.key not in REVISION_FIELDS
    }
    values.update(scope, revision=1 if previous is None else previous.revision + 1, state="active")
    dialect = session.get_bind().dialect.name
    insert = {"postgresql": postgres_insert, "sqlite": sqlite_insert}[dialect]
    session.execute(insert(model).values(**values).on_conflict_do_nothing())
    active = session.scalar(query.where(model.state == "active").with_for_update()
                            .execution_options(populate_existing=True))
    if active is None:
        raise RuntimeError(f"runtime_policy_initialization_conflict:{model.__tablename__}")
    return active
