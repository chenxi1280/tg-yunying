"""Measure planner SQL without recording parameters or private payloads."""
from contextlib import contextmanager
from dataclasses import dataclass, field
import logging
import sys
from time import perf_counter

from sqlalchemy import event


logger = logging.getLogger(__name__)
MILLISECONDS_PER_SECOND = 1000
SLOW_SQL_SECONDS = 1


@dataclass
class PlannerTiming:
    task_id: str
    phase: str
    query_count: int = 0
    sql_seconds: float = 0
    created_count: int = 0
    starts: dict = field(default_factory=dict)

    def before_sql(self, _connection, _cursor, _statement, _parameters, context, _many):
        self.starts[id(context)] = perf_counter()

    def after_sql(self, _connection, _cursor, _statement, _parameters, context, _many):
        elapsed = perf_counter() - self.starts.pop(id(context))
        self.query_count += 1
        self.sql_seconds += elapsed
        if elapsed >= SLOW_SQL_SECONDS:
            logger.info("planner_slow_sql task_id=%s phase=%s took_ms=%d caller=%s",
                self.task_id, self.phase, int(elapsed*MILLISECONDS_PER_SECOND), _caller())


def _caller():
    frame = sys._getframe(1)
    while frame is not None:
        module = str(frame.f_globals.get("__name__", ""))
        if module.startswith("app.") and module != __name__:
            return f"{module}:{frame.f_code.co_name}:{frame.f_lineno}"
        frame = frame.f_back
    return "unavailable"


@contextmanager
def measure_planner_phase(session, task_id, *, phase):
    timing = PlannerTiming(task_id=task_id, phase=phase)
    connections = []

    def attach(_session, _transaction, connection):
        if connection in connections:
            return
        connections.append(connection)
        event.listen(connection, "before_cursor_execute", timing.before_sql)
        event.listen(connection, "after_cursor_execute", timing.after_sql)

    event.listen(session, "after_begin", attach)
    attach(session, None, session.connection())
    started = perf_counter()
    outcome = "failed"
    try:
        yield timing
        outcome = "completed"
    finally:
        event.remove(session, "after_begin", attach)
        for connection in connections:
            event.remove(connection, "before_cursor_execute", timing.before_sql)
            event.remove(connection, "after_cursor_execute", timing.after_sql)
        logger.info("planner_phase task_id=%s phase=%s outcome=%s took_ms=%d sql_ms=%d queries=%d created=%d",
            task_id, phase, outcome, int((perf_counter()-started)*MILLISECONDS_PER_SECOND),
            int(timing.sql_seconds*MILLISECONDS_PER_SECOND), timing.query_count, timing.created_count)
