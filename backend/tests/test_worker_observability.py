from __future__ import annotations

import logging

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import worker as worker_module
from app.database import Base
from app.models import WorkerHeartbeat
from app.timezone import beijing_now
from app.worker_health import (
    REQUIRED_ALL_PROCESS_TYPES,
    check_worker_health,
    required_all_process_types,
    stale_worker_roles,
)

pytestmark = pytest.mark.no_postgres


# ---------------------------------------------------------------------------
# RC-6.2 脱敏
# ---------------------------------------------------------------------------


def test_redact_text_masks_session_token_password_api_hash_and_phone() -> None:
    text = (
        "login failed session=abc123 token: deadbeef password：p@ss "
        "api_hash=xyz webhook_secret=whsec phone +8613800138000 keep=ok"
    )
    redacted = worker_module._redact_text(text)

    for secret in ("abc123", "deadbeef", "p@ss", "xyz", "whsec", "+8613800138000"):
        assert secret not in redacted
    assert "session=***" in redacted
    assert "token:***" in redacted
    assert "keep=ok" in redacted


def test_redaction_filter_covers_msg_and_args() -> None:
    logger = logging.getLogger("test.redaction")
    record = logger.makeRecord(
        "test.redaction",
        logging.INFO,
        __file__,
        1,
        "send failed session=%s",
        ("supersecret",),
        None,
    )

    worker_module.SensitiveDataRedactionFilter().filter(record)

    message = record.getMessage()
    assert "supersecret" not in message
    assert "***" in message


# ---------------------------------------------------------------------------
# RC-6.1 日志初始化
# ---------------------------------------------------------------------------


def test_configure_worker_logging_is_idempotent_and_redacts_output(capsys) -> None:
    root = logging.getLogger()
    original_level = root.level
    # 其他测试可能通过 worker.main() 遗留 marker handler（指向其自身的 capture 流），
    # 先移除保证本测试的输出进入当前 capsys 缓冲。
    for handler in [
        handler
        for handler in root.handlers
        if getattr(handler, "_tgyunying_worker", False)
    ]:
        root.handlers.remove(handler)
    try:
        worker_module._configure_worker_logging()
        worker_module._configure_worker_logging()

        handlers = [
            handler
            for handler in root.handlers
            if getattr(handler, "_tgyunying_worker", False)
        ]
        assert len(handlers) == 1

        logging.getLogger("tgyunying.test").info(
            "worker drained role=planner processed=3 session=secretvalue"
        )
        try:
            raise RuntimeError("trace failed token=trace-secret +8613800138000")
        except RuntimeError:
            logging.getLogger("tgyunying.test").exception("worker failed")
        output = capsys.readouterr().out
        assert "worker drained role=planner processed=3" in output
        assert "secretvalue" not in output
        assert "trace-secret" not in output
        assert "+8613800138000" not in output
    finally:
        for handler in [
            handler
            for handler in root.handlers
            if getattr(handler, "_tgyunying_worker", False)
        ]:
            root.handlers.remove(handler)
        root.setLevel(original_level)


def test_drain_iteration_logs_processed_count(monkeypatch, caplog) -> None:
    monkeypatch.setattr(worker_module, "_record_loop_heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker_module, "_write_local_healthcheck_heartbeat", lambda: None)
    monkeypatch.setattr(worker_module, "drain_once", lambda limit, role=None: 5)

    with caplog.at_level(logging.INFO):
        assert worker_module._drain_worker_iteration("planner", 10, None) is True

    assert any(
        "processed=5" in record.getMessage() and "role=planner" in record.getMessage()
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# RC-6.6 heartbeat freshness 与 all 语义
# ---------------------------------------------------------------------------


def _engine_with_heartbeats(process_types: list[str]):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for process_type in process_types:
            session.add(
                WorkerHeartbeat(
                    worker_id=f"pytest-{process_type}",
                    process_type=process_type,
                    status="active",
                    last_seen_at=beijing_now(),
                )
            )
        session.commit()
    return engine


def test_check_worker_health_all_requires_every_required_role() -> None:
    engine = _engine_with_heartbeats(sorted(REQUIRED_ALL_PROCESS_TYPES))
    factory = lambda: Session(engine)  # noqa: E731 - test factory.

    assert check_worker_health(
        role="all",
        session_factory=factory,
        account_batch_login_mode="enabled",
    ) is True
    assert stale_worker_roles(
        role="all",
        session_factory=factory,
        account_batch_login_mode="enabled",
    ) == set()


def test_check_worker_health_all_fails_when_single_role_missing() -> None:
    partial = [role for role in REQUIRED_ALL_PROCESS_TYPES if role != "search-dispatcher"]
    engine = _engine_with_heartbeats(partial)
    factory = lambda: Session(engine)  # noqa: E731 - test factory.

    assert check_worker_health(
        role="all",
        session_factory=factory,
        account_batch_login_mode="enabled",
    ) is False
    assert stale_worker_roles(
        role="all",
        session_factory=factory,
        account_batch_login_mode="enabled",
    ) == {"search-dispatcher"}


def test_check_worker_health_all_fails_when_role_heartbeat_stale() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for process_type in sorted(REQUIRED_ALL_PROCESS_TYPES):
            # last_seen 远超 2 分钟 freshness 窗口
            from datetime import timedelta

            session.add(
                WorkerHeartbeat(
                    worker_id=f"pytest-{process_type}",
                    process_type=process_type,
                    status="active",
                    last_seen_at=beijing_now() - timedelta(hours=1),
                )
            )
        session.commit()
    factory = lambda: Session(engine)  # noqa: E731 - test factory.

    assert check_worker_health(
        role="all",
        session_factory=factory,
        account_batch_login_mode="enabled",
    ) is False
    assert stale_worker_roles(
        role="all",
        session_factory=factory,
        account_batch_login_mode="enabled",
    ) == set(REQUIRED_ALL_PROCESS_TYPES)


def test_check_worker_health_all_excludes_account_login_when_mode_off() -> None:
    required = required_all_process_types("off")
    assert "account-login" not in required
    engine = _engine_with_heartbeats(sorted(required))
    factory = lambda: Session(engine)  # noqa: E731 - test factory.

    assert check_worker_health(
        role="all",
        session_factory=factory,
        account_batch_login_mode="off",
    ) is True
    assert check_worker_health(
        role="all",
        session_factory=factory,
        account_batch_login_mode="enabled",
    ) is False


def test_check_worker_health_accepts_search_dispatcher_role() -> None:
    engine = _engine_with_heartbeats(["search-dispatcher"])
    factory = lambda: Session(engine)  # noqa: E731 - test factory.

    assert check_worker_health(role="search-dispatcher", session_factory=factory) is True
