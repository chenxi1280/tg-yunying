from __future__ import annotations

import argparse
import logging
import threading
import time
import traceback
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from .config import get_settings
from .database import SessionLocal
from .models import MessageTask, TaskStatus, WorkerHeartbeat
from .services._common import _as_utc, _now
from .services.task_center.heartbeat import record_worker_heartbeat
from .task_queue import get_task_queue
from .services import (
    drain_account_sync_records,
    drain_account_online_keepalive,
    drain_account_security_batches,
    drain_ai_message_memory_maintenance,
    drain_archives,
    drain_continuous_campaigns,
    drain_group_listeners,
    drain_operation_tasks,
    drain_profile_sync_records,
    drain_task_center,
    drain_task_dispatcher,
    drain_task_listener,
    drain_task_metrics,
    drain_task_planner,
    drain_task_recovery,
    dispatch_task,
)
from .services.source_media import drain_source_media_cache
from .services.material_cache import drain_material_cache
from .services.temp_files import cleanup_temp_files

logger = logging.getLogger(__name__)
VALID_WORKER_ROLES = {
    "all",
    "legacy",
    "planner",
    "dispatcher",
    "listener",
    "recovery",
    "account-online",
    "account-security",
    "ai-memory",
    "material-cache",
    "metrics",
}
WORKER_HEALTH_STALE_AFTER = timedelta(minutes=2)


def _task_due(task_id: int) -> bool:
    with SessionLocal() as session:
        task = session.get(MessageTask, task_id)
        if not task or task.status != TaskStatus.QUEUED.value:
            return True
        return _as_utc(task.scheduled_at) <= _as_utc(_now())


def _normalize_role(role: str | None = None) -> str:
    settings = get_settings()
    value = (role or getattr(settings, "worker_role", "all") or "all").strip().lower()
    if value not in VALID_WORKER_ROLES:
        raise ValueError(f"unsupported worker role: {value}")
    return value


def drain_once(limit: int = 100, *, role: str | None = None) -> int:
    selected_role = _normalize_role(role)
    if selected_role == "planner":
        return drain_task_planner(SessionLocal, limit)
    if selected_role == "dispatcher":
        return drain_task_dispatcher(SessionLocal, limit)
    if selected_role == "listener":
        return drain_task_listener(SessionLocal, limit)
    if selected_role == "recovery":
        return drain_task_recovery(SessionLocal, limit)
    if selected_role == "account-online":
        return drain_account_online_keepalive(SessionLocal, limit)
    if selected_role == "account-security":
        return _drain_account_security_once(limit)
    if selected_role == "ai-memory":
        return drain_ai_message_memory_maintenance(SessionLocal, limit)
    if selected_role == "material-cache":
        return drain_material_cache(SessionLocal, limit)
    if selected_role == "metrics":
        return drain_task_metrics(SessionLocal, limit)
    if selected_role == "legacy":
        return _drain_legacy_once(limit)
    return _drain_legacy_once(limit) + drain_task_center(SessionLocal, max(1, limit))


def _drain_legacy_once(limit: int = 100) -> int:
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
    listener_count = 0
    if settings.enable_legacy_campaign_worker:
        listener_count = drain_group_listeners(SessionLocal, max(1, remaining))
    remaining = max(0, remaining - listener_count)
    source_media_count = _safe_optional_drain("source_media", drain_source_media_cache, SessionLocal, max(1, remaining))
    remaining = max(0, remaining - source_media_count)
    material_cache_count = _safe_optional_drain("material_cache", drain_material_cache, SessionLocal, max(1, remaining))
    remaining = max(0, remaining - material_cache_count)
    account_security_count = drain_account_security_batches(SessionLocal, max(1, remaining))
    remaining = max(0, remaining - account_security_count)
    continuous_count = 0
    if settings.enable_legacy_campaign_worker:
        continuous_count = drain_continuous_campaigns(SessionLocal, max(1, remaining))
    remaining = max(0, remaining - continuous_count)
    operation_count = 0
    if settings.enable_legacy_operation_task_worker:
        operation_count = drain_operation_tasks(SessionLocal, max(1, remaining))
    remaining = max(0, remaining - operation_count)
    archive_count = drain_archives(SessionLocal, max(1, remaining))
    _safe_optional_drain("temp_files", cleanup_temp_files)
    return count + profile_count + account_count + account_security_count + listener_count + source_media_count + material_cache_count + continuous_count + operation_count + archive_count


def _drain_account_security_once(limit: int) -> int:
    material_cache_count = drain_material_cache(SessionLocal, max(1, limit))
    remaining = max(1, limit - material_cache_count)
    account_security_count = drain_account_security_batches(SessionLocal, remaining)
    return material_cache_count + account_security_count


def _safe_optional_drain(name: str, func, *args, **kwargs) -> int:
    try:
        return int(func(*args, **kwargs) or 0)
    except SQLAlchemyError:
        logger.warning("optional worker drain skipped name=%s:\n%s", name, traceback.format_exc())
        return 0


def check_worker_health(*, role: str | None = None) -> bool:
    selected_role = _normalize_role(role)
    cutoff = _now() - WORKER_HEALTH_STALE_AFTER
    process_types = _health_process_types(selected_role)
    try:
        with SessionLocal() as session:
            fresh = set(
                session.scalars(
                    select(WorkerHeartbeat.process_type).where(
                        WorkerHeartbeat.process_type.in_(process_types),
                        WorkerHeartbeat.status == "active",
                        WorkerHeartbeat.last_seen_at >= cutoff,
                    )
                )
            )
        return bool(fresh) if selected_role == "all" else process_types <= fresh
    except SQLAlchemyError:
        logger.warning("worker healthcheck failed role=%s:\n%s", selected_role, traceback.format_exc())
        return False


def _health_process_types(role: str) -> set[str]:
    if role == "all":
        return {
            "task_center",
            "planner",
            "dispatcher",
            "listener",
            "recovery",
            "account-online",
            "account-security",
            "ai-memory",
            "material-cache",
            "metrics",
        }
    if role == "legacy":
        return {"legacy"}
    return {role}


def _record_loop_heartbeat(role: str, limit: int) -> None:
    process_type = "task_center" if role == "all" else role
    with SessionLocal() as session:
        record_worker_heartbeat(session, process_type=process_type, metadata={"limit": limit, "source": "worker_loop"})
        session.commit()


def _start_periodic_heartbeat(role: str, limit: int) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_periodic_heartbeat_loop,
        args=(role, limit, stop_event),
        name=f"{role}-heartbeat",
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def _periodic_heartbeat_loop(role: str, limit: int, stop_event: threading.Event) -> None:
    while not stop_event.wait(30):
        try:
            _record_loop_heartbeat(role, limit)
        except Exception:
            logger.warning("worker heartbeat refresh failed role=%s:\n%s", role, traceback.format_exc())


def run_worker(
    *,
    limit: int = 100,
    interval_seconds: float = 2.0,
    max_iterations: int | None = None,
    stop_event: threading.Event | None = None,
    role: str | None = None,
) -> None:
    selected_role = _normalize_role(role)
    heartbeat_stop, heartbeat_thread = _start_periodic_heartbeat(selected_role, limit)
    iterations = 0
    try:
        while (max_iterations is None or iterations < max_iterations) and not (stop_event and stop_event.is_set()):
            try:
                _record_loop_heartbeat(selected_role, limit)
                processed = drain_once(limit, role=selected_role)
                if processed:
                    logger.info("worker drained role=%s processed=%d", selected_role, processed)
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
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TG operations background worker")
    parser.add_argument("--once", action="store_true", help="drain once and exit")
    parser.add_argument("--limit", type=int, default=100, help="max items to drain per iteration")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between drain iterations")
    parser.add_argument("--iterations", type=int, default=None, help="test/dev helper: stop after N iterations")
    parser.add_argument("--role", choices=sorted(VALID_WORKER_ROLES), default=None, help="worker role to drain; defaults to WORKER_ROLE")
    parser.add_argument("--healthcheck", action="store_true", help="exit 0 when this worker role has a fresh heartbeat")
    args = parser.parse_args(argv)
    if args.healthcheck:
        return 0 if check_worker_health(role=args.role) else 1
    if args.once:
        role = _normalize_role(args.role)
        processed = drain_once(args.limit, role=role)
        print(f"role={role} processed={processed}")
        return 0
    try:
        run_worker(limit=args.limit, interval_seconds=args.interval, max_iterations=args.iterations, role=args.role)
    except KeyboardInterrupt:
        logger.info("worker stopped by keyboard interrupt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
