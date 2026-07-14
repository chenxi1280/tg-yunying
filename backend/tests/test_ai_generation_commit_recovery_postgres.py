from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from threading import Barrier
from time import monotonic
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from app.database import Base, SessionLocal, engine
from app.integrations.telegram import OperationResult, SendResult
from app.models import (
    Action,
    AiGroupMessageMemory,
    GroupContextMessage,
    Task,
    TaskAccountDailyCoverage,
    Tenant,
    TgAccount,
    TgAccountOnlineState,
    TgGroup,
    TgGroupAccount,
)
from app.services._common import _now
from app.services.task_center import ai_generation_dispatch, dispatcher
from app.services.task_center import service as task_service
from app.services.task_center.ai_generation_commit import load_generation_batch
from app.services.task_center.ai_generation_dependencies import GenerationDependencies
from app.services.task_center.payloads import SendMessagePayload


pytestmark = pytest.mark.allow_missing_rule_binding
TENANT_ID = 914_003
TASK_ID = "pg-ai-generation-commit-recovery"
GROUP_ID = 914_003
ACCOUNT_ID = 914_003
ACTION_ID = "pg-ai-generation-action"


@dataclass(frozen=True)
class RecoveryScope:
    tenant_id: int
    task_id: str
    group_id: int
    account_id: int
    action_id: str
    coverage_id: str


DEFAULT_SCOPE = RecoveryScope(TENANT_ID, TASK_ID, GROUP_ID, ACCOUNT_ID, ACTION_ID, "pg-ai-generation-coverage")
STALE_SCOPE = RecoveryScope(914_004, "pg-ai-generation-stale", 914_004, 914_004, "pg-ai-generation-stale-action", "pg-ai-generation-stale-coverage")


def test_phase_c_commit_failure_recovers_cached_ai_result_without_second_generation(monkeypatch) -> None:
    Base.metadata.create_all(engine)
    observed = {"provider_calls": 0, "gateway_calls": 0}
    transaction_durations: list[float] = []
    with SessionLocal() as session:
        _listen_for_transaction_durations(session, transaction_durations)
        action, coverage = _seed_reserved_reply_action(session)
        dependencies = _external_dependencies(monkeypatch, session, observed)
        action.status = "pending"
        session.commit()
        claimed, claim_durations = _concurrent_claim_ids()
        assert sum(len(ids) for ids in claimed) == 1
        assert max(claim_durations) < 5.0
        session.refresh(action)
        assert action.status == "executing"
        event.listen(session, "before_commit", _fail_first_ready_commit())

        assert dispatcher.dispatch_action(
            session,
            action,
            generation_dependencies=dependencies,
        ) is True

        session.refresh(action)
        session.refresh(coverage)
        assert action.status == "pending"
        assert action.payload["ai_generation_status"] == "ai_result_persist_unknown"
        assert action.payload["ai_generation_result_cache"]["content"] == "就按这个节奏来"
        assert coverage.state == "reserved"
        assert observed == {"provider_calls": 1, "gateway_calls": 0}
        assert list(session.scalars(select(AiGroupMessageMemory))) == []

        old_claim_owner = action.payload["ai_generation_claim_owner"]
        old_claim_token = action.payload["ai_generation_claim_token"]
        old_request = SimpleNamespace(
            tenant_id=TENANT_ID,
            task_id=TASK_ID,
            group_id=GROUP_ID,
            batch_ids=[action.id],
            claim_owner=old_claim_owner,
            claim_token=old_claim_token,
            attempt_id=action.payload["ai_generation_attempt_id"],
        )
        [action] = dispatcher.claim_actions(session, limit=1, worker_id="recovery-worker")
        assert action.payload["ai_generation_claim_owner"] == "recovery-worker"
        assert action.payload["ai_generation_claim_token"] != old_claim_token
        with pytest.raises(ai_generation_dispatch.GenerationAttemptStale):
            load_generation_batch(session, old_request)
        assert dispatcher.dispatch_action(
            session,
            action,
            generation_dependencies=dependencies,
        ) is True

        assert action.status == "success", action.result
        assert action.payload["ai_generation_status"] == "ready"
        assert action.payload["ai_generation_result_cache"] == {}
        assert observed == {"provider_calls": 1, "gateway_calls": 1}
        memory = session.scalar(select(AiGroupMessageMemory).where(AiGroupMessageMemory.action_id == action.id))
        assert memory.status == "success"
        assert coverage.state == "confirmed"
        assert coverage.confirmed_count == 1
    assert transaction_durations and max(transaction_durations) < 5.0


def test_stale_pre_gateway_generation_reclaims_same_action_slot_and_coverage(monkeypatch) -> None:
    Base.metadata.create_all(engine)
    observed = {"provider_calls": 0, "gateway_calls": 0}
    with SessionLocal() as session:
        action, coverage = _seed_reserved_reply_action(session, STALE_SCOPE)
        action.status = "pending"
        session.commit()
        [action] = dispatcher.claim_actions(session, limit=1, worker_id="stale-worker")
        old_claim_token = action.payload["ai_generation_claim_token"]
        old_request = ai_generation_dispatch._prepare_generation_request(
            session,
            session.get(Task, STALE_SCOPE.task_id),
            [(action, SendMessagePayload.model_validate(action.payload))],
            account=session.get(TgAccount, STALE_SCOPE.account_id),
            credentials=object(),
        )
        action.lease_expires_at = _now() - timedelta(seconds=1)
        session.commit()

        assert task_service._recover_stale_executing_actions(session, timeout_minutes=30) == 1

        session.refresh(action)
        session.refresh(coverage)
        assert action.id == STALE_SCOPE.action_id
        assert action.status == "pending"
        assert action.payload["slot_id"] == "cycle-reply:turn:1"
        assert action.payload["ai_generation_status"] == "pending"
        assert action.result.get("error_code") not in {"execution_timeout", "unknown_after_send"}
        assert coverage.state == "reserved"
        assert coverage.reserved_action_id == action.id
        [action] = dispatcher.claim_actions(session, limit=1, worker_id="new-worker")
        assert action.id == STALE_SCOPE.action_id
        assert action.payload["ai_generation_claim_owner"] == "new-worker"
        assert action.payload["ai_generation_claim_token"] != old_claim_token
        with pytest.raises(ai_generation_dispatch.GenerationAttemptStale):
            load_generation_batch(session, old_request)

        dependencies = _external_dependencies(monkeypatch, session, observed)
        assert dispatcher.dispatch_action(
            session,
            action,
            generation_dependencies=dependencies,
        ) is True
        assert action.status == "success", action.result
        assert observed == {"provider_calls": 1, "gateway_calls": 1}
        assert coverage.state == "confirmed"


def _concurrent_claim_ids() -> tuple[list[list[str]], list[float]]:
    barrier = Barrier(2)

    def claim(worker_id: str) -> tuple[list[str], float]:
        with SessionLocal() as session:
            barrier.wait()
            started_at = monotonic()
            actions = dispatcher.claim_actions(session, limit=1, worker_id=worker_id)
            return [action.id for action in actions], monotonic() - started_at

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(claim, worker_id) for worker_id in ("worker-a", "worker-b")]
        results = [future.result() for future in futures]
    return [item[0] for item in results], [item[1] for item in results]


def _listen_for_transaction_durations(session, durations: list[float]) -> None:
    starts: dict[int, float] = {}

    def started(_session, transaction, _connection) -> None:
        starts[id(transaction)] = monotonic()

    def ended(_session, transaction) -> None:
        if began_at := starts.pop(id(transaction), None):
            durations.append(monotonic() - began_at)

    event.listen(session, "after_begin", started)
    event.listen(session, "after_transaction_end", ended)


def _seed_reserved_reply_action(session, scope: RecoveryScope = DEFAULT_SCOPE):
    timestamp = _now()
    _seed_scope(session, timestamp, scope)
    session.flush()
    context = GroupContextMessage(
        tenant_id=scope.tenant_id,
        group_id=scope.group_id,
        listener_account_id=scope.account_id,
        sender_name="真人用户",
        content="今天按原计划吗？",
        remote_message_id="9001",
        sent_at=timestamp - timedelta(minutes=1),
    )
    session.add(context)
    session.flush()
    action = _new_action(context.id, timestamp, scope)
    session.add(action)
    session.flush()
    coverage = _new_coverage(timestamp, scope)
    action.payload = {**action.payload, "coverage_ledger_id": coverage.id}
    session.add(coverage)
    session.commit()
    return action, coverage


def _seed_scope(session, timestamp, scope: RecoveryScope) -> None:
    session.add(Tenant(id=scope.tenant_id, name=f"AI recovery tenant {scope.tenant_id}"))
    session.flush()
    session.add_all([
        Task(
            id=scope.task_id,
            tenant_id=scope.tenant_id,
            name="AI recovery task",
            type="group_ai_chat",
            status="running",
            type_config={"target_group_id": scope.group_id, "context_bound_schedule_window_seconds": 300},
        ),
        TgAccount(
            id=scope.account_id,
            tenant_id=scope.tenant_id,
            display_name="账号A",
            phone_masked="+8611",
            status="在线",
            session_ciphertext="session-a",
        ),
        TgGroup(
            id=scope.group_id,
            tenant_id=scope.tenant_id,
            tg_peer_id=f"-100{scope.group_id}",
            title="运营群",
            auth_status="已授权运营",
            can_send=True,
            require_review=False,
        ),
    ])
    session.flush()
    session.add_all([
        TgAccountOnlineState(
            tenant_id=scope.tenant_id,
            account_id=scope.account_id,
            desired_online=True,
            online_status="online",
            last_seen_at=timestamp,
            stale_after_at=timestamp + timedelta(minutes=10),
        ),
        TgGroupAccount(tenant_id=scope.tenant_id, group_id=scope.group_id, account_id=scope.account_id, can_send=True),
    ])


def _new_action(context_id: int, timestamp, scope: RecoveryScope) -> Action:
    return Action(
        id=scope.action_id,
        tenant_id=scope.tenant_id,
        task_id=scope.task_id,
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=scope.account_id,
        status="executing",
        scheduled_at=timestamp,
        payload=_reply_payload(context_id, scope),
    )


def _new_coverage(timestamp, scope: RecoveryScope) -> TaskAccountDailyCoverage:
    return TaskAccountDailyCoverage(
        id=scope.coverage_id,
        tenant_id=scope.tenant_id,
        task_id=scope.task_id,
        group_id=scope.group_id,
        account_id=scope.account_id,
        coverage_date=timestamp.date(),
        state="reserved",
        reserved_action_id=scope.action_id,
        targeted_at=timestamp,
    )


def _reply_payload(context_id: int, scope: RecoveryScope) -> dict:
    return {
        "group_id": scope.group_id,
        "target_display": "运营群",
        "message_text": "",
        "review_approved": True,
        "cycle_id": "cycle-reply",
        "slot_id": "cycle-reply:turn:1",
        "turn_index": 1,
        "reply_to_message_id": 9001,
        "reply_target_author": "真人用户",
        "reply_target_preview": "今天按原计划吗？",
        "reply_target_source": "human_context",
        "context_snapshot_message_id": context_id,
        "context_message_ids": [context_id],
        "ai_generation_id": "cycle-reply",
        "ai_generation_status": "pending",
        "ai_generation_history": "真人用户: 今天按原计划吗？",
    }


def _external_dependencies(
    monkeypatch,
    session,
    observed: dict[str, int],
) -> GenerationDependencies:
    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(dispatcher.gateway, "send_message", _reply_sender(session, observed))
    return GenerationDependencies(
        normal_generator=_forbidden_external,
        reply_generator=_reply_generator(observed),
        reply_target_probe=_reply_probe(session),
        reply_messages_fetcher=_reply_fetch(session),
    )


def _fail_first_ready_commit():
    fired = False

    def fail(session) -> None:
        nonlocal fired
        ready = any(
            isinstance(item, Action)
            and dict(item.payload or {}).get("ai_generation_status") == "ready"
            for item in session.dirty
        )
        if ready and not fired:
            fired = True
            raise RuntimeError("injected_phase_c_commit_failure")

    return fail


def _reply_generator(observed: dict[str, int]):
    def generate(session, *_args, **_kwargs):
        assert session.in_transaction() is False
        observed["provider_calls"] += 1
        return ["就按这个节奏来"], 9

    return generate


def _reply_sender(session, observed: dict[str, int]):
    def send(*_args, **_kwargs):
        assert session.in_transaction() is False
        observed["gateway_calls"] += 1
        return SendResult(True, remote_message_id="tg-recovery-1")

    return send


def _reply_probe(session):
    def probe(*_args, **_kwargs):
        assert session.in_transaction() is False
        return OperationResult(True, detail="可引用")

    return probe


def _reply_fetch(session):
    def fetch(*_args, **_kwargs):
        assert session.in_transaction() is False
        return [SimpleNamespace(remote_message_id="9001")]

    return fetch


def _forbidden_external(*_args, **_kwargs):
    raise AssertionError("unexpected external call")
