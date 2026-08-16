from __future__ import annotations

import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import (
    AccountPool,
    Tenant,
    TgAccountLoginBatch,
    TgAccountLoginBatchAttempt,
    TgAccountLoginBatchItem,
)
from app.services._common import _now
from app.services.account_login.state import PhaseClaim, claim_batch_phase
from app.services.account_login.batches import list_login_batches


pytestmark = pytest.mark.no_postgres


@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as session:
        session.add(Tenant(id=1, name="并行登录测试租户"))
        session.add(AccountPool(id=10, tenant_id=1, name="并行目标分组", pool_purpose="normal"))
        session.commit()
    yield factory
    engine.dispose()


def _add_batch(session_factory, *, key: str, item_count: int) -> int:
    with session_factory() as session:
        batch = TgAccountLoginBatch(
            tenant_id=1,
            pool_id=10,
            created_by="测试操作员",
            recipient_user_id=20,
            idempotency_key=key,
            request_fingerprint=key.ljust(64, "0")[:64],
            total_count=item_count,
            reason="并行登录测试",
            trace_id=f"trace-{key}",
        )
        session.add(batch)
        session.flush()
        for line_no in range(1, item_count + 1):
            item = TgAccountLoginBatchItem(
                batch_id=batch.id,
                tenant_id=1,
                line_no=line_no,
                phone_masked=f"+120****{line_no:04d}",
                phone_fingerprint=f"{key}-phone-{line_no}",
                phone_fingerprint_version=1,
                phone_ciphertext="encrypted-phone",
                code_url_ciphertext="encrypted-url",
                code_source_host="tgbotchecker",
                code_source_uuid_fingerprint=f"{key}-uuid-{line_no}",
                code_source_uuid_hint=f"hint-{line_no}",
                route_hint="create",
            )
            session.add(item)
            session.flush()
            attempt = TgAccountLoginBatchAttempt(
                item_id=item.id,
                batch_id=batch.id,
                tenant_id=1,
                execution_generation=1,
            )
            session.add(attempt)
            session.flush()
            item.current_attempt_id = attempt.id
        session.commit()
        return batch.id


def test_same_batch_can_claim_multiple_items(session_factory) -> None:
    batch_id = _add_batch(session_factory, key="same-batch", item_count=3)

    with session_factory() as session:
        first = claim_batch_phase(session, batch_id)
    with session_factory() as session:
        second = claim_batch_phase(session, batch_id)

    assert first is not None
    assert second is not None
    assert first.item_id != second.item_id


def test_future_retry_on_first_item_does_not_block_next_line(session_factory) -> None:
    batch_id = _add_batch(session_factory, key="future-retry", item_count=2)
    with session_factory() as session:
        first = session.scalar(select(TgAccountLoginBatchItem).where(
            TgAccountLoginBatchItem.batch_id == batch_id,
            TgAccountLoginBatchItem.line_no == 1,
        ))
        first.status = "waiting"
        first.next_retry_at = _now() + timedelta(minutes=1)
        session.commit()

    with session_factory() as session:
        claim = claim_batch_phase(session, batch_id)

    assert claim is not None
    assert claim.item_id != first.id


def test_missing_current_attempt_is_exposed_instead_of_silently_skipped(session_factory) -> None:
    batch_id = _add_batch(session_factory, key="missing-attempt", item_count=1)
    with session_factory() as session:
        item = session.scalar(select(TgAccountLoginBatchItem).where(
            TgAccountLoginBatchItem.batch_id == batch_id,
        ))
        item.current_attempt_id = None
        session.commit()

    with session_factory() as session, pytest.raises(RuntimeError, match="current attempt is inconsistent"):
        claim_batch_phase(session, batch_id)


def test_parallel_drain_runs_two_item_phases_at_once(session_factory, monkeypatch) -> None:
    from app.services.account_login import drain

    _add_batch(session_factory, key="parallel-drain", item_count=2)
    barrier = threading.Barrier(2)
    started: list[int] = []
    started_lock = threading.Lock()

    def execute_in_parallel(_session, claim) -> None:
        with started_lock:
            started.append(claim.item_id)
        barrier.wait(timeout=2)

    settings = SimpleNamespace(
        account_batch_login_mode="enabled",
        account_batch_login_worker_concurrency=2,
    )
    monkeypatch.setattr(drain, "get_settings", lambda: settings)
    monkeypatch.setattr(drain, "execute_local_phase", execute_in_parallel)

    processed = drain.drain_account_login_batches(session_factory, 2, code_client=object())

    assert processed == 2
    assert len(set(started)) == 2


def test_stalled_remote_phase_does_not_block_drain_loop(monkeypatch) -> None:
    from app.services.account_login import drain

    claims = [
        PhaseClaim(1, 2, 3, 1, 1, "send_code", "lease-token-a"),
        PhaseClaim(1, 4, 5, 1, 1, "send_code", "lease-token-b"),
    ]
    released = threading.Event()

    def stalled_remote_phase(_session_factory, _claim, _client) -> None:
        if _claim.item_id == 2:
            released.wait(timeout=0.2)

    monkeypatch.setattr(drain, "PHASE_JOIN_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(drain, "execute_remote_phase", stalled_remote_phase)

    started_at = time.monotonic()
    drain._execute_claims(lambda: None, claims, object())
    elapsed = time.monotonic() - started_at
    released.set()

    assert elapsed < 0.15


def test_claims_are_fair_before_reusing_same_batch(session_factory) -> None:
    from app.services.account_login import drain

    first_batch = _add_batch(session_factory, key="fair-first", item_count=2)
    second_batch = _add_batch(session_factory, key="fair-second", item_count=2)

    claims = drain._claim_fair_phases(session_factory, 3)

    assert {claims[0].batch_id, claims[1].batch_id} == {first_batch, second_batch}
    assert len(claims) == 3


def test_task_list_prioritizes_active_batches_over_newer_history(session_factory) -> None:
    active_batch = _add_batch(session_factory, key="older-active", item_count=1)
    completed_batch = _add_batch(session_factory, key="newer-completed", item_count=1)
    with session_factory() as session:
        session.get(TgAccountLoginBatch, active_batch).status = "running"
        session.get(TgAccountLoginBatch, completed_batch).status = "completed"
        session.commit()

    with session_factory() as session:
        batches = list_login_batches(session, 1, limit=2, offset=0)

    assert [batch.id for batch in batches] == [active_batch, completed_batch]
