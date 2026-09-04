"""Keep one generation worker's recovery out of the other adapter's jobs."""
from sqlalchemy import select, true

from app.models import GenerationJob, Task


def generation_task_filter(task_type: str | None):
    if task_type is None:
        return true()
    return select(Task.id).where(
        Task.id == GenerationJob.task_id,
        Task.tenant_id == GenerationJob.tenant_id,
        Task.type == task_type,
    ).exists()
