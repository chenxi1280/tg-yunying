from __future__ import annotations

from sqlalchemy import text

from app.database import SessionLocal
from app.services.task_center.executors import search_click


def test_search_click_finalize_restarts_serializable_after_solver_reads() -> None:
    with SessionLocal() as session:
        session.execute(text("SELECT 1"))

        search_click._restart_serializable_finalize_transaction(session)

        assert session.execute(text("SELECT 1")).scalar_one() == 1
        session.rollback()
