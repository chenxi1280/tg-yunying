from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, timedelta
from threading import Barrier
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    AiGenerationContractAudit,
    AiGroupMessageMemory,
    ContentMixCycleSlot,
    ContentMixObligation,
    ExecutionAttempt,
    GroupContextMessage,
    Task,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
    TgAccount,
)
from app.services._common import _now
from app.services.task_center import ai_generation_dispatch, ai_generation_pipeline, dispatcher
from app.services.task_center.ai_generator import AiGenerationUnavailable, GeneratedContent
from app.services.task_center.direct_check_in import requires_direct_check_in
from app.services.task_center.ai_generation_worker import drain_ai_generation
from app.services.task_center.payloads import SendMessagePayload
from tests.ai_generation_phase_test_support import (
    account_content_generator,
    barrier_generator,
    forbidden_external,
    forbidden_normal_generation,
    generation_dependencies,
    invalid_reply_dependencies,
    invalidate_reply_target,
    normal_generator,
    profile_generator,
    profile_sender,
    reply_fetch,
    reply_generator,
    reply_probe,
    reply_sender,
    seed_reply_action,
    seed_reserved_normal_batch,
    seed_reserved_reply_action,
)


pytestmark = pytest.mark.no_postgres


def _dispatch_with_generation_worker(
    session: Session,
    action: Action,
    monkeypatch,
    *,
    generation_dependencies,
) -> bool:
    payload = dict(action.payload or {})
    if not str(payload.get("message_text") or "").strip():
        action.status = "pending"
        action.claim_owner = ""
        action.claim_token = ""
        action.scheduled_at = _now()
        session.commit()
        monkeypatch.setattr(
            "app.services.task_center.ai_generation_worker.credentials_for_account",
            lambda *_args, **_kwargs: object(),
        )
        drain_ai_generation(
            lambda: Session(session.get_bind()),
            limit=1,
            dependencies=generation_dependencies,
        )
        session.refresh(action)
        if action.status != "pending":
            return True
        action.status = "executing"
        session.commit()
    return dispatcher.dispatch_action(
        session,
        action,
        generation_dependencies=generation_dependencies,
    )


def test_missing_mask_check_in_keeps_valid_reply_binding() -> None:
    payload = SendMessagePayload(
        chat_id="-1007",
        group_id=7,
        message_text="",
        ai_generation_status="pending",
        coverage_ledger_id="coverage-1",
        content_source="mask_missing_check_in",
        mask_status="missing",
        reply_to_message_id=991,
    )

    assert requires_direct_check_in(payload) is True


def test_generation_dependencies_are_isolated_between_concurrent_pipelines() -> None:
    barrier = Barrier(2)

    def run(label: str) -> str:
        engine = create_engine("sqlite:///:memory:", future=True)
        with Session(engine) as session:
            dependencies = generation_dependencies(
                normal_generator=barrier_generator(barrier, label),
            )
            request = SimpleNamespace(
                tenant_id=1,
                is_reply=False,
                config={"generation_slots": [{"slot_id": label}]},
                target_label="",
                history="",
                reply_targets=[],
            )
            contents, _tokens = ai_generation_pipeline._generate_stage(
                session,
                request,
                [0],
                stage="direct_configured_model",
                dependencies=dependencies,
            )
            return contents[0]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, ["worker-a", "worker-b"]))

    assert results == ["worker-a", "worker-b"]

def test_dispatch_reply_generation_uses_reply_provider_without_db_transaction(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    observed: dict[str, object] = {}
    with Session(engine) as session:
        action = seed_reply_action(session, now_value)
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", reply_sender(session, observed))

        assert _dispatch_with_generation_worker(
            session,
            action,
            monkeypatch,
            generation_dependencies=generation_dependencies(
                normal_generator=forbidden_normal_generation,
                reply_generator=reply_generator(observed),
                reply_target_probe=reply_probe(session),
                reply_message_fetcher=reply_fetch(session),
            ),
        ) is True

        assert action.status == "success", action.result
        assert observed == {
            "provider_transaction": False,
            "reply_target": 9001,
            "gateway_transaction": False,
            "sent_reply_target": 9001,
        }
        assert action.payload["message_text"] == "就按这个节奏来"


def _seed_own_history_reply_action(session: Session) -> Action:
    action = seed_reply_action(session, _now())
    prior = Action(
        id="successful-own-history",
        tenant_id=1,
        task_id=action.task_id,
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=11,
        status="success",
        payload={"group_id": 7, "message_text": "前一条已发消息"},
    )
    session.add(prior)
    session.flush()
    session.add(ExecutionAttempt(
        action_id=prior.id,
        status="success",
        remote_message_id="9002",
    ))
    action.payload = {
        **(action.payload or {}),
        "reply_to_message_id": 9002,
        "reply_target_preview": "前一条已发消息",
        "reply_target_source": "own_history",
    }
    session.commit()
    return action


def _own_history_dependencies(session: Session, observed: dict[str, object]):
    def generate(_session, _tenant_id, config, *, reply_targets, **_kwargs):
        observed["reply_target"] = reply_targets[0]["message_id"]
        return [GeneratedContent(
            "接着这条聊",
            slot_id=config["generation_slots"][0]["slot_id"],
            sequence_index=1,
            reply_to_sequence_index=1,
        )], 5

    def fetch_exact(_account_id, _peer_id, message_id, *_args, **_kwargs):
        observed["exact_fetch_target"] = str(message_id)
        return SimpleNamespace(remote_message_id=str(message_id))

    return generation_dependencies(
        reply_generator=generate,
        reply_target_probe=reply_probe(session),
        reply_message_fetcher=fetch_exact,
    )


def test_dispatch_reply_generation_accepts_authoritative_own_history(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    observed: dict[str, object] = {}
    with Session(engine) as session:
        action = _seed_own_history_reply_action(session)
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", reply_sender(session, observed))

        assert _dispatch_with_generation_worker(
            session,
            action,
            monkeypatch,
            generation_dependencies=_own_history_dependencies(session, observed),
        ) is True

        assert action.status == "success", action.result
        assert observed["reply_target"] == 9002
        assert observed["exact_fetch_target"] == "9002"
        assert observed["sent_reply_target"] == 9002
        assert action.payload["message_text"] == "接着这条聊"


@pytest.mark.parametrize(
    ("invalidation", "expected_code"),
    [
        ("local_missing", "cross_group_content_scope_mismatch"),
        ("permission", "reply_target_missing"),
        ("remote_missing", "reply_target_missing"),
    ],
)
def test_invalid_reply_target_skips_ai_and_gateway_and_releases_coverage(
    monkeypatch,
    invalidation: str,
    expected_code: str,
) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    with Session(engine) as session:
        action, coverage = seed_reserved_reply_action(session, now_value)
        invalidate_reply_target(session, action, invalidation=invalidation, now_value=now_value)
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", forbidden_external)

        assert _dispatch_with_generation_worker(
            session,
            action,
            monkeypatch,
            generation_dependencies=invalid_reply_dependencies(session, invalidation),
        ) is True

        assert action.status == "failed"
        assert action.result["error_code"] == expected_code
        assert action.payload["ai_generation_status"] == expected_code
        if invalidation == "remote_missing":
            assert action.result["reply_target_observation"] == "remote_missing_or_inaccessible"
            assert action.result["reply_target_message_id"] == "9001"
        assert coverage.state == "ready"
        assert coverage.reserved_action_id is None


def test_reply_queue_age_does_not_invalidate_available_target(
    monkeypatch,
) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    observed: dict[str, object] = {}
    with Session(engine) as session:
        action = seed_reply_action(session, now_value)
        action.created_at = now_value - timedelta(minutes=10)
        session.commit()
        monkeypatch.setattr(
            dispatcher,
            "credentials_for_account",
            lambda *_args, **_kwargs: object(),
        )
        monkeypatch.setattr(
            dispatcher.gateway,
            "send_message",
            reply_sender(session, observed),
        )

        assert _dispatch_with_generation_worker(
            session,
            action,
            monkeypatch,
            generation_dependencies=generation_dependencies(
                normal_generator=forbidden_normal_generation,
                reply_generator=reply_generator(observed),
                reply_target_probe=reply_probe(session),
                reply_message_fetcher=reply_fetch(session),
            ),
        ) is True

        assert action.status == "success"
        assert action.result["telegram_msg_id"] == "tg-reply-1"


@pytest.mark.parametrize(
    "outputs",
    [
        [GeneratedContent("一号", sequence_index=1)],
        [
            GeneratedContent("一号", sequence_index=1),
            GeneratedContent("二号", sequence_index=2),
            GeneratedContent("额外", sequence_index=3),
        ],
        [GeneratedContent("一号", sequence_index=1), GeneratedContent("二号", sequence_index=1)],
        [GeneratedContent("二号", sequence_index=2), GeneratedContent("一号", sequence_index=1)],
    ],
)
def test_invalid_normal_mapping_fails_only_current_late_bound_slot(monkeypatch, outputs) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        actions, _coverages = seed_reserved_normal_batch(session, _now(), bind_coverage=False)
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", forbidden_external)

        assert _dispatch_with_generation_worker(
            session,
            actions[0],
            monkeypatch,
            generation_dependencies=generation_dependencies(
                normal_generator=normal_generator(session, outputs),
            ),
        ) is True

        assert [action.status for action in actions] == ["failed", "executing"]
        assert actions[0].result["error_code"].startswith("ai_generation_")
        assert not actions[1].result
        audit = session.query(AiGenerationContractAudit).one()
        assert audit.expected_slot_count == 1
        assert audit.error_code.startswith("ai_generation_")
        assert audit.received_slot_count <= len(outputs)


def test_normal_batch_rejects_swapped_slot_ids_despite_correct_sequences(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    outputs = [GeneratedContent(
        "一号",
        slot_id="cycle-normal:turn:2",
        sequence_index=1,
    )]
    with Session(engine) as session:
        actions, _coverages = seed_reserved_normal_batch(session, _now(), bind_coverage=False)
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", forbidden_external)

        assert _dispatch_with_generation_worker(
            session,
            actions[0],
            monkeypatch,
            generation_dependencies=generation_dependencies(
                normal_generator=normal_generator(session, outputs),
            ),
        ) is True

        assert [action.status for action in actions] == ["failed", "executing"]
        assert actions[0].result["error_code"] == "ai_generation_slot_mapping_mismatch"
        assert not actions[1].result
        assert session.query(AiGenerationContractAudit).count() == 1


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [("account_id", 999), ("coverage_ledger_id", "wrong-coverage")],
)
def test_normal_batch_rejects_tampered_fixed_slot_binding(
    monkeypatch,
    field: str,
    invalid_value,
) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    original_slot = ai_generation_dispatch._generation_slot

    def tampered_slot(action, payload, index, **kwargs):
        slot = original_slot(action, payload, index, **kwargs)
        return {**slot, field: invalid_value} if index == 1 else slot

    monkeypatch.setattr(ai_generation_dispatch, "_generation_slot", tampered_slot)
    outputs = [GeneratedContent(
        "一号",
        slot_id="cycle-normal:turn:1",
        sequence_index=1,
    )]
    with Session(engine) as session:
        actions, _coverages = seed_reserved_normal_batch(session, _now(), bind_coverage=False)
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", forbidden_external)

        assert _dispatch_with_generation_worker(
            session,
            actions[0],
            monkeypatch,
            generation_dependencies=generation_dependencies(
                normal_generator=normal_generator(session, outputs),
            ),
        ) is True

        assert [action.status for action in actions] == ["failed", "executing"]
        assert actions[0].result["error_code"] == "ai_generation_slot_mapping_mismatch"
        assert not actions[1].result
        assert session.query(AiGenerationContractAudit).count() == 1


def test_daily_coverage_repeats_exact_check_in_per_coverage_without_dedupe(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    observed = {"provider_calls": 0, "gateway_calls": 0}
    with Session(engine) as session:
        actions, coverages = seed_reserved_normal_batch(session, _now(), with_masks=False)
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", profile_sender(session, observed))

        assert _dispatch_with_generation_worker(
            session,
            actions[0],
            monkeypatch,
            generation_dependencies=generation_dependencies(
                normal_generator=profile_generator(session, observed),
            ),
        ) is True

        assert actions[0].status == "success", actions[0].result.get("error_code")
        assert actions[0].payload["message_text"] == "签到"
        assert actions[0].payload["content_source"] == "mask_missing_check_in"
        assert coverages[0].state == "confirmed"
        assert coverages[1].state == "reserved"
        assert coverages[1].reserved_action_id == actions[1].id
        assert observed == {"provider_calls": 0, "gateway_calls": 1}

        assert _dispatch_with_generation_worker(
            session,
            actions[1],
            monkeypatch,
            generation_dependencies=generation_dependencies(
                normal_generator=profile_generator(session, observed),
            ),
        ) is True
        assert actions[1].status == "success"
        assert actions[1].payload["message_text"] == "签到"
        assert actions[1].payload["content_source"] == "mask_missing_check_in"
        assert actions[0].payload["ai_message_memory_id"] != actions[1].payload["ai_message_memory_id"]
        assert coverages[1].state == "confirmed"
        assert observed == {"provider_calls": 0, "gateway_calls": 2}


def test_daily_coverage_sends_exact_check_in_without_ai_provider(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    observed = {"provider_calls": 0, "gateway_calls": 0}
    with Session(engine) as session:
        actions, coverages = seed_reserved_normal_batch(session, _now(), with_masks=False)
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", profile_sender(session, observed))

        assert _dispatch_with_generation_worker(
            session,
            actions[0],
            monkeypatch,
            generation_dependencies=generation_dependencies(
                normal_generator=profile_generator(session, observed),
            ),
        ) is True

        assert actions[0].status == "success", actions[0].result
        assert actions[0].payload["message_text"] == "签到"
        assert actions[0].payload["act_type"] == "check_in"
        assert actions[0].payload["generation_source"] == "mask_missing_check_in"
        assert actions[0].payload["content_source"] == "mask_missing_check_in"
        assert actions[0].payload["human_quality_decision"] == "mask_missing_check_in"
        assert actions[0].payload["ai_generation_tokens"] == 0
        assert actions[0].payload["ai_message_memory_id"]
        assert coverages[0].state == "confirmed"
        assert observed == {"provider_calls": 0, "gateway_calls": 1}


def test_daily_coverage_rejects_same_account_check_in_within_ten_days(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    observed = {"provider_calls": 0, "gateway_calls": 0}
    with Session(engine) as session:
        actions, coverages = seed_reserved_normal_batch(session, _now(), with_masks=False)
        session.add(AiGroupMessageMemory(
            id="recent-account-check-in",
            tenant_id=1,
            group_id=7,
            task_id="older-task",
            action_id="older-action",
            account_id=actions[0].account_id,
            raw_text="签到",
            normalized_text="签到",
            text_fingerprint="recent-account-check-in",
            status="success",
            planned_at=_now() - timedelta(days=1),
        ))
        session.commit()
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", profile_sender(session, observed))

        assert _dispatch_with_generation_worker(
            session,
            actions[0],
            monkeypatch,
            generation_dependencies=generation_dependencies(
                normal_generator=profile_generator(session, observed),
            ),
        ) is True

        assert actions[0].status == "failed"
        assert actions[0].result["error_code"] == "direct_check_in_10d_duplicate"
        assert actions[0].payload.get("message_text") != "签到"
        assert coverages[0].state == "ready"
        assert observed == {"provider_calls": 0, "gateway_calls": 0}


def test_direct_check_in_preserves_admission_probe_fields_written_after_payload_validation() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        actions, _coverages = seed_reserved_normal_batch(session, _now(), with_masks=False)
        action = actions[0]
        stale_payload = SendMessagePayload.model_validate(action.payload)
        action.payload = {
            **action.payload,
            "group_bot_post_follow_visibility_probe": True,
            "group_bot_admission_id": 51,
            "admission_version": 1,
        }

        ai_generation_dispatch.ensure_send_message_content(
            session,
            action,
            session.get(TgAccount, action.account_id),
            payload=stale_payload,
            dependencies=generation_dependencies(),
        )

        assert action.payload["group_bot_post_follow_visibility_probe"] is True
        assert action.payload["group_bot_admission_id"] == 51
        assert action.payload["admission_version"] == 1


def test_daily_coverage_replaces_unsent_ai_content_with_direct_check_in(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    observed = {"provider_calls": 0, "gateway_calls": 0}
    with Session(engine) as session:
        actions, coverages = seed_reserved_normal_batch(session, _now(), with_masks=False)
        from app.services.task_center.ai_message_memory import reserve_group_ai_message

        old_memory = reserve_group_ai_message(
            session,
            tenant_id=1,
            group_id=7,
            task_id=actions[0].task_id,
            account_id=actions[0].account_id,
            raw_text="晚上好啊 今天有空哈 价格咋说",
        )
        old_memory.action_id = actions[0].id
        old_payload = dict(actions[0].payload or {})
        old_payload.update({
            "message_text": "晚上好啊 今天有空哈 价格咋说",
            "ai_generation_status": "ready",
            "ai_generation_tokens": 37,
            "generation_source": "ai_provider",
            "ai_message_memory_id": old_memory.id,
        })
        actions[0].payload = old_payload
        session.commit()
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", profile_sender(session, observed))

        assert _dispatch_with_generation_worker(
            session,
            actions[0],
            monkeypatch,
            generation_dependencies=generation_dependencies(
                normal_generator=profile_generator(session, observed),
            ),
        ) is True

        assert actions[0].status == "success", actions[0].result
        assert actions[0].payload["message_text"] == "签到"
        assert actions[0].payload["generation_source"] == "mask_missing_check_in"
        assert actions[0].payload["ai_generation_tokens"] == 0
        assert old_memory.status == "expired_before_send"
        assert old_memory.quality_decision == "superseded_by_direct_check_in"
        assert coverages[0].state == "confirmed"
        assert observed == {"provider_calls": 0, "gateway_calls": 1}


def test_daily_coverage_replan_rejects_same_account_check_in_within_ten_days() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        actions, coverages = seed_reserved_normal_batch(session, _now(), with_masks=False)
        first = ai_generation_dispatch.ensure_send_message_content(
            session,
            actions[0],
            session.get(TgAccount, actions[0].account_id),
            payload=SendMessagePayload.model_validate(actions[0].payload),
            dependencies=generation_dependencies(),
        )
        first_memory_id = first.ai_message_memory_id
        actions[0].status = "failed"
        second = Action(
            id="action-normal-generation-replan",
            tenant_id=1,
            task_id=actions[0].task_id,
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=actions[0].account_id,
            status="executing",
            scheduled_at=_now(),
            payload={
                **dict(actions[0].payload or {}),
                "message_text": "",
                "ai_message_memory_id": "",
                "ai_generation_status": "pending",
            },
        )
        coverages[0].reserved_action_id = second.id
        session.add(second)
        session.commit()

        with pytest.raises(AiGenerationUnavailable, match="direct_check_in_10d_duplicate"):
            ai_generation_dispatch.ensure_send_message_content(
                session,
                second,
                session.get(TgAccount, second.account_id),
                payload=SendMessagePayload.model_validate(second.payload),
                dependencies=generation_dependencies(),
            )
        second_status = second.status
        second_error = second.result["error_code"]

    assert first_memory_id
    assert second_status == "failed"
    assert second_error == "direct_check_in_10d_duplicate"


def test_ready_normal_generation_expires_when_new_context_arrives() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    with Session(engine) as session:
        actions, _coverages = seed_reserved_normal_batch(session, now_value)
        memory, payload = _ready_action_with_new_context(
            session,
            actions[0],
            now_value=now_value,
            memory_id="memory-superseded-context",
            text="旧上下文正文",
        )

        refreshed = ai_generation_dispatch._invalidate_superseded_normal_generation(
            session,
            session.get(Task, actions[0].task_id),
            actions[0],
            payload=payload,
        )

        assert actions[0].id == "action-reply-generation"
        assert refreshed.ai_generation_status == "pending"
        assert refreshed.message_text == ""
        assert refreshed.ai_message_memory_id == ""
        assert memory.status == "expired_before_send"
        assert memory.result["error_code"] == "generation_context_superseded"


def test_ready_normal_generation_requeues_same_slot_when_context_changes() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    with Session(engine) as session:
        actions, coverages = seed_reserved_normal_batch(session, now_value)
        memory, payload = _ready_action_with_new_context(
            session,
            actions[0],
            now_value=now_value,
            memory_id="memory-context-requeue",
            text="旧正文",
        )

        requeued = ai_generation_dispatch.requeue_normal_generation_after_context_change(
            session,
            session.get(Task, actions[0].task_id),
            actions[0],
            payload=payload,
        )

        assert requeued is True
        assert actions[0].status == "pending"
        assert actions[0].payload["message_text"] == ""
        assert actions[0].payload["ai_generation_status"] == "pending"
        assert actions[0].payload["ai_message_memory_id"] == ""
        assert actions[0].result["context_superseded_requeue_count"] == 1
        assert memory.status == "expired_before_send"
        assert coverages[0].state == "reserved"
        assert coverages[0].reserved_action_id == actions[0].id
        assert actions[1].status == "executing"


def _ready_action_with_new_context(
    session: Session,
    action: Action,
    *,
    now_value,
    memory_id: str,
    text: str,
) -> tuple[AiGroupMessageMemory, SendMessagePayload]:
    old_context = session.scalar(
        select(GroupContextMessage).where(GroupContextMessage.group_id == 7)
    )
    memory = AiGroupMessageMemory(
        id=memory_id,
        tenant_id=1,
        group_id=7,
        task_id=action.task_id,
        action_id=action.id,
        account_id=action.account_id,
        raw_text=text,
        normalized_text=text,
        text_fingerprint=memory_id,
        status="reserved",
        planned_at=now_value,
    )
    session.add_all([memory, GroupContextMessage(
        tenant_id=1,
        group_id=7,
        listener_account_id=11,
        content="更新真人上下文",
        remote_message_id=f"new-{memory_id}",
        sent_at=now_value + timedelta(seconds=1),
    )])
    payload = SendMessagePayload.model_validate({
        **(action.payload or {}),
        "message_text": text,
        "ai_generation_status": "ready",
        "ai_message_memory_id": memory.id,
        "context_snapshot_message_id": old_context.id,
        "context_message_ids": [old_context.id],
    })
    action.payload = payload.model_dump(mode="json")
    session.commit()
    return memory, payload


def test_normal_generation_batch_contains_only_current_action() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        actions, _coverages = seed_reserved_normal_batch(session, _now())
        payload = SendMessagePayload.model_validate(actions[0].payload)

        batch = ai_generation_dispatch._pending_generation_batch(
            session,
            actions[0],
            payload,
        )

        assert [action.id for action, _item in batch] == [actions[0].id]


def test_generation_request_uses_planner_chat_mode_instead_of_history_heuristic() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        actions, _coverages = seed_reserved_normal_batch(session, _now())
        payload = SendMessagePayload.model_validate({
            **actions[0].payload,
            "chat_mode": "idle_warmup",
            "ai_generation_history": "真人用户: 有历史不代表 reply",
        })
        actions[0].payload = payload.model_dump(mode="json")

        request = ai_generation_dispatch._generation_request(
            session.get(Task, actions[0].task_id),
            [(actions[0], payload)],
            session.get(TgAccount, actions[0].account_id),
            session=session,
            credentials=object(),
            peer_id="-1007",
            attempt_id="attempt-chat-mode",
        )

        assert request.chat_mode == "idle_warmup"


def test_generation_runtime_derives_fallback_obligation_and_deadline() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    with Session(engine) as session:
        actions, _coverages = seed_reserved_normal_batch(session, now_value)
        action = actions[0]
        _seed_runtime_generation_contract(session, action, now_value)
        session.commit()

        assert ai_generation_dispatch._content_obligation_fallback_ready(session, action) is True
        assert ai_generation_dispatch._latest_safe_send_at(session, action) == now_value + timedelta(hours=1)

        session.add(ContentMixObligation(
            id="obligation-runtime-1",
            tenant_id=1,
            content_mix_contract_id="contract-runtime-1",
            content_mix_scope_key="scope-runtime-1",
            obligation_source="policy_min",
            obligation_kind="image",
            obligation_ordinal=1,
            assigned_cycle_slot_id="cycle-slot-runtime-1",
            assigned_action_id=action.id,
            required_count=1,
        ))
        session.commit()

        assert ai_generation_dispatch._content_obligation_fallback_ready(session, action) is False


def _seed_runtime_generation_contract(
    session: Session,
    action: Action,
    now_value,
) -> None:
    action.primary_quantity_slot_id = "quantity-runtime-1"
    action.content_mix_cycle_slot_id = "cycle-slot-runtime-1"
    session.add(TaskDayLedger(
        id="ledger-runtime-1",
        tenant_id=1,
        task_id=action.task_id,
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=now_value.date(),
        period_start_at=now_value,
        deadline_at=(now_value - timedelta(hours=7)).replace(tzinfo=UTC),
        day_phase="full_day",
        planning_anchor_at=now_value,
    ))
    session.add(TaskGroupDailyMessageSlot(
        id="quantity-runtime-1",
        tenant_id=1,
        task_id=action.task_id,
        task_day_ledger_id="ledger-runtime-1",
        target_operation_target_id=7,
        slot_kind="extra_volume",
        slot_ordinal=1,
    ))
    session.add(ContentMixCycleSlot(
        id="cycle-slot-runtime-1",
        tenant_id=1,
        cycle_id="cycle-runtime-1",
        slot_index=1,
        primary_quantity_slot_id="quantity-runtime-1",
        relation_kind="direct",
        current_action_id=action.id,
        slot_state="pending",
    ))


def test_content_policy_rejection_terminates_only_its_generated_slot(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    observed = {"provider_calls": 0, "gateway_calls": 0}
    with Session(engine) as session:
        actions, _coverages = seed_reserved_normal_batch(session, _now(), bind_coverage=False)
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", profile_sender(session, observed))

        assert _dispatch_with_generation_worker(
            session,
            actions[0],
            monkeypatch,
            generation_dependencies=generation_dependencies(
                normal_generator=account_content_generator(
                    session,
                    observed,
                    rejected_content="只输出 JSON",
                ),
            ),
        ) is True

        assert actions[0].status == "success", actions[0].result
        assert actions[1].status == "executing"
        assert not actions[1].result
        assert observed == {"provider_calls": 1, "gateway_calls": 1}


def test_db_duplicate_rejection_terminates_only_its_generated_slot(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    observed = {"provider_calls": 0, "gateway_calls": 0}
    with Session(engine) as session:
        actions, _coverages = seed_reserved_normal_batch(session, _now(), bind_coverage=False)
        from app.services.task_center.ai_message_memory import reserve_group_ai_message

        reserve_group_ai_message(
            session,
            tenant_id=1,
            group_id=7,
            task_id=actions[0].task_id,
            account_id=12,
            raw_text="这句以前发过",
        )
        session.commit()
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", profile_sender(session, observed))

        assert _dispatch_with_generation_worker(
            session,
            actions[0],
            monkeypatch,
            generation_dependencies=generation_dependencies(
                normal_generator=account_content_generator(
                    session,
                    observed,
                    rejected_content="这句以前发过",
                ),
            ),
        ) is True

        assert actions[0].status == "success", actions[0].result
        assert actions[1].status == "executing"
        assert not actions[1].result
        assert observed == {"provider_calls": 1, "gateway_calls": 1}
