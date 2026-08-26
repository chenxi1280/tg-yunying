from __future__ import annotations

import argparse
import logging
import os
import re
import signal
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from .config import get_settings
from .database import SessionLocal
from .dispatcher_lifecycle import (
    DispatcherLifecycle,
    create_dispatcher_lifecycle,
)
from .models import MessageTask, TaskStatus
from .services._common import _as_utc, _now
from .worker_health import VALID_WORKER_ROLES
from .services.task_center.heartbeat import (
    record_worker_heartbeat,
    retire_worker_heartbeat,
)
from .services.task_center.dispatch_runtime_control import (
    dispatch_writer_allowed,
    record_dispatcher_shard_heartbeat,
)
from .services.task_center.planner_resource_sampler import (
    record_planner_resource_sample_if_due,
)
from .telethon_lifecycle import shutdown_telethon_lifecycle_strict
from .telethon_lifecycle import TelethonClientLifecycle
from .worker_role_loaders import (
    cleanup_temp_files,
    dispatch_task,
    dispatcher_runtime_reservation_count,
    drain_account_login_batches,
    drain_account_login_reconciliation,
    drain_account_post_login_initializations,
    drain_account_online_keepalive,
    drain_account_security_batches,
    drain_account_sync_records,
    drain_ai_generation,
    drain_ai_message_memory_maintenance,
    drain_archives,
    drain_continuous_campaigns,
    drain_group_listeners,
    drain_material_cache,
    drain_notification_outbox,
    drain_operation_tasks,
    drain_profile_sync_records,
    drain_search_dispatcher,
    drain_source_media_cache,
    drain_task_center,
    drain_task_dispatcher,
    drain_task_listener,
    drain_task_metrics,
    drain_task_planner,
    drain_task_recovery,
    drain_voice_profile_generation,
    get_image_verification_runtime,
    get_task_queue,
)
from .worker_periodic_heartbeat import (
    PeriodicHeartbeatThreads,
    start_periodic_heartbeats,
)

logger = logging.getLogger(__name__)
LOCAL_HEALTHCHECK_FILE = "/tmp/tgyunying-worker-heartbeat"

# RC-6.2 日志脱敏（硬约束）：session/密码/2FA/验证码/token/手机号不得明文输出。
_SENSITIVE_KEY_VALUE_PATTERN = re.compile(
    r"(?i)\b((?:raw_)?(?:session|token|password|api_hash|secret|verification_code|"
    r"two_fa|two_fa_password|webhook_secret))\s*([=：:])\s*[^\s,;，；)）]+"
)
_PHONE_NUMBER_PATTERN = re.compile(r"\+\d{7,15}")
_IDLE_HEARTBEAT_TICKS = 60


def _redact_text(value: str) -> str:
    value = _SENSITIVE_KEY_VALUE_PATTERN.sub(r"\1\2***", value)
    return _PHONE_NUMBER_PATTERN.sub("+***", value)


class SensitiveDataRedactionFilter(logging.Filter):
    """在 handler 边界统一脱敏：先完成 %-格式化再整体脱敏，避免破坏占位符。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - 格式化失败时退回分别脱敏。
            record.msg = _redact_text(str(record.msg))
            if record.args:
                args = record.args if isinstance(record.args, tuple) else (record.args,)
                record.args = tuple(
                    _redact_text(arg) if isinstance(arg, str) else arg for arg in args
                )
            return True
        record.msg = _redact_text(message)
        record.args = None
        return True


class SensitiveDataFormatter(logging.Formatter):
    """对最终日志文本再次脱敏，覆盖 formatter 追加的异常 traceback。"""

    def format(self, record: logging.LogRecord) -> str:
        return _redact_text(super().format(record))


def _configure_worker_logging() -> None:
    """RC-6.1：worker 统一 stdout 结构化日志；级别由 WORKER_LOG_LEVEL 控制，默认 INFO。

    没有该初始化时 root logger 默认 WARNING，全部 INFO drain 日志静默丢失
    （生产表现为 docker logs 0 字节）。
    """
    root = logging.getLogger()
    if any(
        isinstance(handler, logging.StreamHandler) and getattr(handler, "_tgyunying_worker", False)
        for handler in root.handlers
    ):
        return
    level_name = (os.getenv("WORKER_LOG_LEVEL") or "INFO").strip().upper()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        SensitiveDataFormatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    handler.addFilter(SensitiveDataRedactionFilter())
    handler.set_name("tgyunying-worker")
    handler._tgyunying_worker = True  # noqa: SLF001 - 幂等标记
    root.addHandler(handler)
    root.setLevel(getattr(logging, level_name, logging.INFO))


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
    TelethonClientLifecycle.set_runtime_role(selected_role)
    if not _dispatch_write_allowed(selected_role):
        return 0
    if selected_role == "planner":
        return drain_task_planner(SessionLocal, limit)
    if selected_role == "dispatcher":
        return drain_task_dispatcher(SessionLocal, limit)
    if selected_role == "search-dispatcher":
        return drain_search_dispatcher(SessionLocal, limit)
    if selected_role == "listener":
        return drain_task_listener(SessionLocal, limit)
    if selected_role == "recovery":
        return drain_task_recovery(SessionLocal, limit)
    if selected_role == "account-online":
        return drain_account_online_keepalive(SessionLocal, limit)
    if selected_role == "account-security":
        return _drain_account_security_once(limit)
    if selected_role == "account-login":
        return _drain_account_login_once(limit)
    if selected_role == "ai-memory":
        return drain_ai_message_memory_maintenance(SessionLocal, limit)
    if selected_role == "ai-generation":
        return drain_ai_generation(SessionLocal, limit)
    if selected_role == "voice-profile":
        settings = get_settings()
        return drain_voice_profile_generation(
            SessionLocal,
            limit=limit,
            reconcile_interval_seconds=settings.voice_profile_reconcile_interval_seconds,
            reconcile_limit=settings.voice_profile_reconcile_batch_limit,
        )
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
    account_login_count = _drain_account_login_once(max(1, remaining))
    remaining = max(0, remaining - account_login_count)
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
    return count + profile_count + account_count + account_security_count + account_login_count + listener_count + source_media_count + material_cache_count + continuous_count + operation_count + archive_count


def _drain_account_security_once(limit: int) -> int:
    return drain_account_security_batches(SessionLocal, max(1, limit))


def _drain_account_login_once(limit: int) -> int:
    settings = get_settings()
    post_init_count = drain_account_post_login_initializations(SessionLocal, max(1, limit))
    if settings.account_batch_login_mode == "off":
        return post_init_count
    reconcile_count = drain_account_login_reconciliation(SessionLocal, max(1, limit))
    notification_count = drain_notification_outbox(SessionLocal, max(1, limit))
    if settings.account_batch_login_mode == "reconcile_only":
        return reconcile_count + notification_count + post_init_count
    batch_count = drain_account_login_batches(SessionLocal, max(1, limit))
    return batch_count + reconcile_count + notification_count + post_init_count


def _safe_optional_drain(name: str, func, *args, **kwargs) -> int:
    try:
        return int(func(*args, **kwargs) or 0)
    except SQLAlchemyError:
        logger.warning("optional worker drain skipped name=%s:\n%s", name, traceback.format_exc())
        return 0


def check_worker_health(*, role: str | None = None) -> bool:
    # RC-6.6：健康判定统一走 worker_health 单一实现（freshness + all=全部必需 role）。
    from .worker_health import check_worker_health as _check

    return _check(role=_normalize_role(role), session_factory=SessionLocal)


def _record_loop_heartbeat(role: str, limit: int) -> None:
    process_type = "task_center" if role == "all" else role
    settings = get_settings()
    with SessionLocal() as session:
        heartbeat = record_worker_heartbeat(
            session,
            process_type=process_type,
            metadata=_worker_heartbeat_metadata(settings, limit, role=role),
        )
        if role == "dispatcher":
            record_dispatcher_shard_heartbeat(
                session,
                settings,
                worker_id=heartbeat.worker_id,
            )
        session.commit()


def _worker_heartbeat_metadata(
    settings,
    limit: int,
    *,
    role: str,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "limit": limit,
        "source": "worker_loop",
        "account_batch_login_worker_concurrency": settings.account_batch_login_worker_concurrency,
        "dispatch_contract_version": str(
            getattr(settings, "dispatch_rebuild_contract_version", "") or ""
        ),
    }
    if role == "planner":
        metadata.update({
            "planner_projection_contract": "v2",
            "planner_resource_sample_contract": "v1",
        })
    if role == "listener":
        metadata["listener_snapshot_contract"] = "v1"
    if role == "dispatcher":
        metadata["source_pacing_admission_contract"] = "v1"
    return metadata


def _retire_loop_heartbeat(role: str, limit: int) -> None:
    process_type = "task_center" if role == "all" else role
    with SessionLocal() as session:
        retired = retire_worker_heartbeat(
            session,
            process_type=process_type,
            reason="worker_loop_exit",
        )
        session.commit()
    if not retired:
        logger.warning(
            "worker heartbeat retirement found no row role=%s limit=%d",
            role,
            limit,
        )


def _dispatch_write_allowed(role: str) -> bool:
    settings = get_settings()
    if getattr(settings, "app_env", "") != "production":
        return True
    with SessionLocal() as session:
        return dispatch_writer_allowed(session, settings, role=role)


def _write_local_healthcheck_heartbeat() -> None:
    heartbeat_path = Path(os.getenv("WORKER_LOCAL_HEALTHCHECK_FILE", LOCAL_HEALTHCHECK_FILE))
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="ascii",
            dir=heartbeat_path.parent,
            prefix=f"{heartbeat_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp_file:
            tmp_path = Path(tmp_file.name)
            tmp_file.write(str(int(time.time())))
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        tmp_path.replace(heartbeat_path)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _start_periodic_heartbeat(
    role: str,
    limit: int,
) -> tuple[threading.Event, PeriodicHeartbeatThreads]:
    return start_periodic_heartbeats(
        database_refresh=lambda: _record_loop_heartbeat(role, limit),
        local_refresh=_write_local_healthcheck_heartbeat,
        database_failure=lambda _exc: logger.warning(
            "worker database heartbeat refresh failed role=%s", role, exc_info=True
        ),
        local_failure=lambda _exc: logger.warning(
            "worker local heartbeat refresh failed role=%s", role, exc_info=True
        ),
        thread_name_prefix=role,
    )


def _periodic_heartbeat_loop(role: str, limit: int, stop_event: threading.Event) -> None:
    while not stop_event.wait(30):
        try:
            _record_loop_heartbeat(role, limit)
        except Exception:
            logger.warning("worker database heartbeat refresh failed role=%s:\n%s", role, traceback.format_exc())
        try:
            _write_local_healthcheck_heartbeat()
        except Exception:
            logger.warning("worker local heartbeat refresh failed role=%s:\n%s", role, traceback.format_exc())


def run_worker(
    *,
    limit: int = 100,
    interval_seconds: float = 2.0,
    max_iterations: int | None = None,
    stop_event: threading.Event | None = None,
    role: str | None = None,
    dispatcher_lifecycle: DispatcherLifecycle | None = None,
) -> None:
    selected_role = _normalize_role(role)
    lifecycle = dispatcher_lifecycle or _dispatcher_lifecycle(selected_role)
    heartbeat_stop, heartbeat_thread = _start_periodic_heartbeat(selected_role, limit)
    try:
        _run_worker_loop(
            role=selected_role,
            limit=limit,
            interval_seconds=interval_seconds,
            max_iterations=max_iterations,
            stop_event=stop_event,
            lifecycle=lifecycle,
        )
        if _lifecycle_is_stopping(lifecycle, stop_event):
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=1)
        _finish_dispatcher_lifecycle(
            lifecycle,
            stop_event,
            selected_role,
            limit,
        )
    finally:
        _stop_and_retire_heartbeat(
            heartbeat_stop,
            heartbeat_thread,
            role=selected_role,
            limit=limit,
        )


def _run_worker_loop(
    *,
    role: str,
    limit: int,
    interval_seconds: float,
    max_iterations: int | None,
    stop_event: threading.Event | None,
    lifecycle: DispatcherLifecycle | None,
) -> None:
    iterations = 0
    while _worker_loop_active(iterations, max_iterations, stop_event):
        if not _drain_worker_iteration(role, limit, lifecycle):
            break
        iterations += 1
        if max_iterations is not None and iterations >= max_iterations:
            break
        wait_seconds = max(0.1, interval_seconds)
        if _wait_for_worker_iteration(stop_event, wait_seconds):
            break


def _worker_loop_active(
    iterations: int,
    max_iterations: int | None,
    stop_event: threading.Event | None,
) -> bool:
    has_budget = max_iterations is None or iterations < max_iterations
    return has_budget and not bool(stop_event and stop_event.is_set())


def _drain_worker_iteration(
    role: str,
    limit: int,
    lifecycle: DispatcherLifecycle | None,
) -> bool:
    try:
        _record_loop_heartbeat(role, limit)
        _write_local_healthcheck_heartbeat()
        if lifecycle is not None:
            lifecycle.acknowledge_successor()
        started = time.monotonic()
        processed = drain_once(limit, role=role)
        took_ms = int((time.monotonic() - started) * 1000)
        _record_resource_sample(role, processed, took_ms)
        if processed:
            _drain_worker_iteration.idle_ticks = 0  # type: ignore[attr-defined]
            logger.info("worker drained role=%s processed=%d took_ms=%d", role, processed, took_ms)
        else:
            ticks = getattr(_drain_worker_iteration, "idle_ticks", 0) + 1
            _drain_worker_iteration.idle_ticks = ticks  # type: ignore[attr-defined]
            if ticks % _IDLE_HEARTBEAT_TICKS == 1:
                logger.info("worker idle role=%s ticks=%d took_ms=%d", role, ticks, took_ms)
            else:
                logger.debug("worker idle role=%s took_ms=%d", role, took_ms)
        if lifecycle is None:
            return True
        lifecycle.observe_after_batch()
        return lifecycle.state == "active"
    except Exception:
        logger.error("worker drain failed role=%s:\n%s", role, traceback.format_exc())
        return True


def _record_resource_sample(role: str, processed: int, took_ms: int) -> None:
    if role != "planner":
        return
    with SessionLocal() as session:
        created = record_planner_resource_sample_if_due(
            session,
            process_type=role,
            drain_metrics={"processed_count": processed, "took_ms": took_ms},
        )
        if created:
            session.commit()


def _wait_for_worker_iteration(
    stop_event: threading.Event | None,
    wait_seconds: float,
) -> bool:
    if stop_event is not None:
        return stop_event.wait(wait_seconds)
    time.sleep(wait_seconds)
    return False


def _stop_and_retire_heartbeat(
    heartbeat_stop: threading.Event,
    heartbeat_thread: threading.Thread,
    *,
    role: str,
    limit: int,
) -> None:
    heartbeat_stop.set()
    heartbeat_thread.join(timeout=1)
    try:
        _retire_loop_heartbeat(role, limit)
    except Exception:
        logger.error(
            "worker heartbeat retirement failed role=%s:\n%s",
            role,
            traceback.format_exc(),
        )


def _lifecycle_is_stopping(
    lifecycle: DispatcherLifecycle | None,
    stop_event: threading.Event | None,
) -> bool:
    if lifecycle is None:
        return False
    return lifecycle.state != "active" or bool(
        stop_event and stop_event.is_set()
    )


def _finish_dispatcher_lifecycle(
    lifecycle: DispatcherLifecycle | None,
    stop_event: threading.Event | None,
    role: str,
    limit: int,
) -> None:
    if lifecycle is None:
        return
    if stop_event and stop_event.is_set() and lifecycle.state == "active":
        lifecycle.request_stop("stop_event", automatic=False)
    if lifecycle.state == "active":
        return
    lifecycle.drain_until_safe(
        lambda metadata: _record_lifecycle_heartbeat(
            role,
            limit,
            metadata,
        ),
        stop_event,
    )


def _dispatcher_lifecycle(role: str) -> DispatcherLifecycle | None:
    if role != "dispatcher":
        return None
    settings = get_settings()
    if not settings.image_verification_contract_enabled:
        return None
    runtime = get_image_verification_runtime(
        settings.image_verification_model_concurrency
    )
    return create_dispatcher_lifecycle(
        settings,
        SessionLocal,
        runtime,
        shutdown_telethon_lifecycle_strict,
        dispatcher_runtime_reservation_count,
    )


def _record_lifecycle_heartbeat(
    role: str,
    limit: int,
    metadata: dict[str, object],
) -> None:
    settings = get_settings()
    with SessionLocal() as session:
        heartbeat = record_worker_heartbeat(
            session,
            process_type=role,
            metadata={
                **_worker_heartbeat_metadata(settings, limit, role=role),
                **metadata,
            },
        )
        if role == "dispatcher":
            state = "recycling" if metadata.get("state") != "active" else "live"
            record_dispatcher_shard_heartbeat(
                session,
                settings,
                worker_id=heartbeat.worker_id,
                state=state,
            )
        session.commit()


def _install_dispatcher_signal_handlers(
    lifecycle: DispatcherLifecycle | None,
    stop_event: threading.Event,
) -> dict[int, object]:
    if lifecycle is None:
        return {}
    previous: dict[int, object] = {}

    def handle(signum, _frame):  # noqa: ANN001 - signal callback contract.
        lifecycle.request_stop(
            signal.Signals(signum).name.lower(),
            automatic=False,
        )
        stop_event.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, handle)
    return previous


def _restore_signal_handlers(previous: dict[int, object]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def main(argv: list[str] | None = None) -> int:
    _configure_worker_logging()
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
    selected_role = _normalize_role(args.role)
    TelethonClientLifecycle.set_runtime_role(selected_role)
    stop_event = threading.Event()
    lifecycle = _dispatcher_lifecycle(selected_role)
    previous_handlers = _install_dispatcher_signal_handlers(
        lifecycle,
        stop_event,
    )
    try:
        run_worker(
            limit=args.limit,
            interval_seconds=args.interval,
            max_iterations=args.iterations,
            stop_event=stop_event,
            role=selected_role,
            dispatcher_lifecycle=lifecycle,
        )
    except KeyboardInterrupt:
        logger.info("worker stopped by keyboard interrupt")
    finally:
        _restore_signal_handlers(previous_handlers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
