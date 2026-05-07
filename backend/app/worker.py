from __future__ import annotations

import logging
import traceback
from datetime import UTC, datetime

from .database import SessionLocal
from .models import MessageTask, TaskStatus
from .task_queue import get_task_queue
from .services import dispatch_task, drain_account_sync_records, drain_archives, drain_group_listeners, drain_profile_sync_records

logger = logging.getLogger(__name__)


def _task_due(task_id: int) -> bool:
    with SessionLocal() as session:
        task = session.get(MessageTask, task_id)
        if not task or task.status != TaskStatus.QUEUED.value:
            return True
        scheduled_at = task.scheduled_at.replace(tzinfo=UTC) if task.scheduled_at.tzinfo is None else task.scheduled_at
        return scheduled_at <= datetime.now(UTC)


def drain_once(limit: int = 100) -> int:
    queue = get_task_queue()
    scan_limit = max(limit, queue.size())
    deferred: list[int] = []
    count = 0
    scanned = 0
    while count < limit and scanned < scan_limit:
        task_id = queue.dequeue()
        if task_id is None:
            break
        scanned += 1
        if not _task_due(task_id):
            deferred.append(task_id)
            continue
        try:
            dispatch_task(SessionLocal, task_id)
            count += 1
        except Exception:
            logger.error("dispatch_task(%d) failed:\n%s", task_id, traceback.format_exc())
    for task_id in deferred:
        queue.enqueue(task_id)
    remaining = max(1, limit - count)
    profile_count = drain_profile_sync_records(SessionLocal, remaining)
    remaining = max(0, remaining - profile_count)
    account_count = drain_account_sync_records(SessionLocal, max(1, remaining))
    remaining = max(0, remaining - account_count)
    listener_count = drain_group_listeners(SessionLocal, max(1, remaining))
    remaining = max(0, remaining - listener_count)
    archive_count = drain_archives(SessionLocal, max(1, remaining))
    return count + profile_count + account_count + listener_count + archive_count


if __name__ == "__main__":
    processed = drain_once()
    print(f"processed={processed}")
