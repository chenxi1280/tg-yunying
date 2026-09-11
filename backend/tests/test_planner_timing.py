import logging

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.services.task_center.planner_timing import measure_planner_phase

pytestmark = pytest.mark.no_postgres


def test_metrics_omit_query_values_and_remove_listeners_on_error(caplog):
    with Session(create_engine("sqlite://")) as session:
        caplog.set_level(logging.INFO)
        with pytest.raises(ValueError, match="expected"):
            with measure_planner_phase(session, "task", phase="test") as metric:
                assert session.scalar(text("select :value"), {"value": "private-value"}) == "private-value"
                raise ValueError("expected")
        assert metric.query_count == 1
        session.scalar(text("select 2"))
        assert metric.query_count == 1
        assert "private-value" not in caplog.text
        assert "outcome=failed" in caplog.text


def test_metrics_continue_across_planner_transaction_commit():
    with Session(create_engine("sqlite://")) as session:
        with measure_planner_phase(session, "task", phase="test") as metric:
            session.scalar(text("select 1"))
            session.commit()
            session.scalar(text("select 2"))
        assert metric.query_count == 2
        session.commit()
        session.scalar(text("select 3"))
        assert metric.query_count == 2
