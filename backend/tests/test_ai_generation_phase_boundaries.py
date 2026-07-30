from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, AiGenerationContractAudit, AiGroupMessageMemory, GroupContextMessage, Task, TgAccount
from app.services._common import _now
from app.services.task_center import ai_generation_dispatch, ai_generation_pipeline, dispatcher
from app.services.task_center.ai_generator import GeneratedContent
from app.services.task_center.direct_check_in import requires_direct_check_in
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

        assert dispatcher.dispatch_action(
            session,
            action,
            generation_dependencies=generation_dependencies(
                normal_generator=forbidden_normal_generation,
                reply_generator=reply_generator(observed),
                reply_target_probe=reply_probe(session),
                reply_messages_fetcher=reply_fetch(session),
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


@pytest.mark.parametrize(
    ("invalidation", "expected_code"),
    [
        ("local_missing", "reply_target_missing"),
        ("stale", "reply_target_stale"),
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

        assert dispatcher.dispatch_action(
            session,
            action,
            generation_dependencies=invalid_reply_dependencies(session, invalidation),
        ) is True

        assert action.status == "failed"
        assert action.result["error_code"] == expected_code
        assert action.payload["ai_generation_status"] == expected_code
        assert coverage.state == "ready"
        assert coverage.reserved_action_id is None


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
def test_invalid_normal_batch_mapping_fails_all_slots_without_gateway(monkeypatch, outputs) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        actions, _coverages = seed_reserved_normal_batch(session, _now(), bind_coverage=False)
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", forbidden_external)

        assert dispatcher.dispatch_action(
            session,
            actions[0],
            generation_dependencies=generation_dependencies(
                normal_generator=normal_generator(session, outputs),
            ),
        ) is True

        assert [action.status for action in actions] == ["failed", "failed"]
        assert all(action.result["error_code"].startswith("ai_generation_output_") for action in actions)
        audit = session.query(AiGenerationContractAudit).one()
        assert audit.expected_slot_count == 2
        assert audit.error_code.startswith("ai_generation_output_")
        if len(outputs) != 2:
            assert audit.received_slot_count == len(outputs)


def test_normal_batch_rejects_swapped_slot_ids_despite_correct_sequences(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    outputs = [
        GeneratedContent("一号", sequence_index=1),
        GeneratedContent("二号", sequence_index=2),
    ]
    outputs[0].slot_id = "cycle-normal:turn:2"
    outputs[1].slot_id = "cycle-normal:turn:1"
    with Session(engine) as session:
        actions, _coverages = seed_reserved_normal_batch(session, _now(), bind_coverage=False)
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", forbidden_external)

        assert dispatcher.dispatch_action(
            session,
            actions[0],
            generation_dependencies=generation_dependencies(
                normal_generator=normal_generator(session, outputs),
            ),
        ) is True

        assert [action.status for action in actions] == ["failed", "failed"]
        assert all(
            action.result["error_code"] == "ai_generation_slot_mapping_mismatch"
            for action in actions
        )
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

    def tampered_slot(action, payload, index):
        slot = original_slot(action, payload, index)
        return {**slot, field: invalid_value} if index == 1 else slot

    monkeypatch.setattr(ai_generation_dispatch, "_generation_slot", tampered_slot)
    outputs = [
        GeneratedContent("一号", slot_id="cycle-normal:turn:1", sequence_index=1),
        GeneratedContent("二号", slot_id="cycle-normal:turn:2", sequence_index=2),
    ]
    with Session(engine) as session:
        actions, _coverages = seed_reserved_normal_batch(session, _now(), bind_coverage=False)
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", forbidden_external)

        assert dispatcher.dispatch_action(
            session,
            actions[0],
            generation_dependencies=generation_dependencies(
                normal_generator=normal_generator(session, outputs),
            ),
        ) is True

        assert [action.status for action in actions] == ["failed", "failed"]
        assert all(
            action.result["error_code"] == "ai_generation_slot_mapping_mismatch"
            for action in actions
        )
        assert session.query(AiGenerationContractAudit).count() == 1


def test_daily_coverage_repeats_exact_check_in_per_coverage_without_dedupe(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    observed = {"provider_calls": 0, "gateway_calls": 0}
    with Session(engine) as session:
        actions, coverages = seed_reserved_normal_batch(session, _now(), with_masks=False)
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", profile_sender(session, observed))

        assert dispatcher.dispatch_action(
            session,
            actions[0],
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

        assert dispatcher.dispatch_action(
            session,
            actions[1],
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

        assert dispatcher.dispatch_action(
            session,
            actions[0],
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

        assert dispatcher.dispatch_action(
            session,
            actions[0],
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


def test_daily_coverage_replan_reserves_new_direct_check_in_memory() -> None:
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

        replanned = ai_generation_dispatch.ensure_send_message_content(
            session,
            second,
            session.get(TgAccount, second.account_id),
            payload=SendMessagePayload.model_validate(second.payload),
            dependencies=generation_dependencies(),
        )

    assert replanned.message_text == "签到"
    assert replanned.ai_message_memory_id != first_memory_id


def test_ready_normal_generation_expires_when_new_context_arrives() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    with Session(engine) as session:
        actions, _coverages = seed_reserved_normal_batch(session, now_value)
        old_context = session.scalar(
            select(GroupContextMessage).where(GroupContextMessage.group_id == 7)
        )
        memory = AiGroupMessageMemory(
            id="memory-superseded-context",
            tenant_id=1,
            group_id=7,
            task_id=actions[0].task_id,
            action_id=actions[0].id,
            account_id=actions[0].account_id,
            raw_text="旧上下文正文",
            normalized_text="旧上下文正文",
            text_fingerprint="memory-superseded-context",
            status="reserved",
            planned_at=now_value,
        )
        new_context = GroupContextMessage(
            tenant_id=1,
            group_id=7,
            listener_account_id=11,
            sender_name="真人用户",
            content="更新真人上下文",
            remote_message_id="9002",
            sent_at=now_value + timedelta(seconds=1),
        )
        session.add_all([memory, new_context])
        payload = {
            **(actions[0].payload or {}),
            "message_text": "旧上下文正文",
            "ai_generation_status": "ready",
            "ai_message_memory_id": memory.id,
            "context_snapshot_message_id": old_context.id,
            "context_message_ids": [old_context.id],
        }
        actions[0].payload = payload
        session.commit()

        refreshed = ai_generation_dispatch._invalidate_superseded_normal_generation(
            session,
            session.get(Task, actions[0].task_id),
            actions[0],
            SendMessagePayload.model_validate(payload),
        )

        assert actions[0].id == "action-reply-generation"
        assert refreshed.ai_generation_status == "pending"
        assert refreshed.message_text == ""
        assert refreshed.ai_message_memory_id == ""
        assert memory.status == "expired_before_send"
        assert memory.result["error_code"] == "generation_context_superseded"


def test_content_policy_rejection_terminates_only_its_generated_slot(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    observed = {"provider_calls": 0, "gateway_calls": 0}
    with Session(engine) as session:
        actions, _coverages = seed_reserved_normal_batch(session, _now(), bind_coverage=False)
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", profile_sender(session, observed))

        assert dispatcher.dispatch_action(
            session,
            actions[0],
            generation_dependencies=generation_dependencies(
                normal_generator=account_content_generator(
                    session,
                    observed,
                    rejected_content="只输出 JSON",
                ),
            ),
        ) is True

        assert actions[0].status == "success", actions[0].result
        assert actions[1].status == "failed"
        assert actions[1].result["error_code"] == "content_rejected"
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

        assert dispatcher.dispatch_action(
            session,
            actions[0],
            generation_dependencies=generation_dependencies(
                normal_generator=account_content_generator(
                    session,
                    observed,
                    rejected_content="这句以前发过",
                ),
            ),
        ) is True

        assert actions[0].status == "success", actions[0].result
        assert actions[1].status == "failed"
        assert actions[1].result["error_code"] == "duplicate_message"
        assert observed == {"provider_calls": 1, "gateway_calls": 1}
