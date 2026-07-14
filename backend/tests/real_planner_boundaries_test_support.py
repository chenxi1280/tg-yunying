from types import SimpleNamespace

from app.services.task_center import service as task_service
from app.services.task_center.executors import group_ai_chat


def configure_real_planner_test(monkeypatch, timestamp, transaction_chunks: list[int]) -> None:
    monkeypatch.setattr(group_ai_chat, "_now", lambda: timestamp)
    monkeypatch.setattr(
        group_ai_chat,
        "get_settings",
        lambda: SimpleNamespace(daily_coverage_plan_batch_limit=50),
    )
    original_plan_batch = task_service._plan_due_task_batch

    def observe_plan_transaction(*args, **kwargs):
        result = original_plan_batch(*args, **kwargs)
        if result[1] > 0:
            transaction_chunks.append(result[1])
        return result

    monkeypatch.setattr(task_service, "_plan_due_task_batch", observe_plan_transaction)
