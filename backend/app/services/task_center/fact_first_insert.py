from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert


def insert_do_nothing(
    session,
    model,
    values: dict,
    *,
    columns: tuple[str, ...],
) -> None:
    table = model.__table__
    statement = (
        pg_insert(table)
        if session.get_bind().dialect.name == "postgresql"
        else sqlite_insert(table)
    )
    session.execute(statement.values(**values).on_conflict_do_nothing(
        index_elements=list(columns),
    ))


__all__ = ["insert_do_nothing"]
