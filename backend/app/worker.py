from __future__ import annotations

import argparse
import logging
import threading
import time
import traceback
from sqlalchemy.exc import SQLAlchemyError
from .config import get_settings
from .database import SessionLocal
from .models import MessageTask, TaskStatus
from .services._common import _as_utc, _now
from .task_queue import get_task_queue
from .services import (
    drain_account_sync_records,
    drain_archives,
    drain_continuous_campaigns,
    drain_group_listeners,
    drain_operation_tasks,
    drain_profile_sync_records,
    drain_task_center,
    dispatch_task,
)
from .services.source_media import drain_source_media_cache
from .services.material_cache import drain_material_cache
from .services.temp_files import cleanup_temp_files

logger = logging.getLogger(__name__)


def _task_due(task_id: int) -> bool:
    with SessionLocal() as session:
        task = session.get(MessageTask, task_id)
        if not task or task.status != TaskStatus.QUEUED.value:
            return True
        return _as_utc(task.scheduled_at) <= _as_utc(_now())


def drain_once(limit: int = 100) -> int:
    settings = get_settings()
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
    source_media_count = _safe_optional_drain("source_media", drain_source_media_cache, SessionLocal, max(1, remaining))
    remaining = max(0, remaining - source_media_count)
    material_cache_count = _safe_optional_drain("material_cache", drain_material_cache, SessionLocal, max(1, remaining))
    remaining = max(0, remaining - material_cache_count)
    continuous_count = 0
    if settings.enable_legacy_campaign_worker:
        continuous_count = drain_continuous_campaigns(SessionLocal, max(1, remaining))
    remaining = max(0, remaining - continuous_count)
    operation_count = 0
    if settings.enable_legacy_operation_task_worker:
        operation_count = drain_operation_tasks(SessionLocal, max(1, remaining))
    remaining = max(0, remaining - operation_count)
    task_center_count = drain_task_center(SessionLocal, max(1, remaining))
    remaining = max(0, remaining - task_center_count)
    archive_count = drain_archives(SessionLocal, max(1, remaining))
    temp_cleanup_count = _safe_optional_drain("temp_files", cleanup_temp_files)
    return count + profile_count + account_count + listener_count + source_media_count + material_cache_count + continuous_count + operation_count + task_center_count + archive_count + temp_cleanup_count


def _safe_optional_drain(name: str, func, *args, **kwargs) -> int:
    try:
        return int(func(*args, **kwargs) or 0)
    except SQLAlchemyError:
        logger.warning("optional worker drain skipped name=%s:\n%s", name, traceback.format_exc())
        return 0


def run_worker(
    *,
    limit: int = 100,
    interval_seconds: float = 2.0,
    max_iterations: int | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    iterations = 0
    while (max_iterations is None or iterations < max_iterations) and not (stop_event and stop_event.is_set()):
        try:
            processed = drain_once(limit)
            if processed:
                logger.info("worker drained processed=%d", processed)
        except Exception:
            logger.error("worker drain failed:\n%s", traceback.format_exc())
        iterations += 1
        if max_iterations is not None and iterations >= max_iterations:
            break
        wait_seconds = max(0.1, interval_seconds)
        if stop_event:
            if stop_event.wait(wait_seconds):
                break
        else:
            time.sleep(wait_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TG operations background worker")
    parser.add_argument("--once", action="store_true", help="drain once and exit")
    parser.add_argument("--limit", type=int, default=100, help="max items to drain per iteration")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between drain iterations")
    parser.add_argument("--iterations", type=int, default=None, help="test/dev helper: stop after N iterations")
    args = parser.parse_args(argv)
    if args.once:
        processed = drain_once(args.limit)
        print(f"processed={processed}")
        return 0
    try:
        run_worker(limit=args.limit, interval_seconds=args.interval, max_iterations=args.iterations)
    except KeyboardInterrupt:
        logger.info("worker stopped by keyboard interrupt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
