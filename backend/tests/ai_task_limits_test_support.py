import pytest
from sqlalchemy import select

from app.integrations.telegram import OperationResult, SendResult
from app.models import OperationTarget, TgGroup, TgGroupAccount
from app.services.task_center import dispatcher
from app.services.task_center.ai_generation_dependencies import GenerationDependencies
from app.services.task_center.ai_generator import GeneratedContent


def configure_closed_loop_dispatch(monkeypatch) -> GenerationDependencies:
    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        dispatcher.gateway,
        "ensure_channel_membership",
        lambda *_args, **_kwargs: OperationResult(True, "已处理", detail="joined"),
    )
    monkeypatch.setattr(
        dispatcher.gateway,
        "probe_target_capabilities",
        lambda *_args, **_kwargs: OperationResult(True, detail="可发言"),
    )
    def sender(*_args, **_kwargs):
        return SendResult(True, remote_message_id="tg-ok")

    monkeypatch.setattr(dispatcher.gateway, "send_message", sender)
    monkeypatch.setattr(dispatcher.gateway, "send_message_to_target", sender)

    def normal_generator(_session, _tenant_id, config, *, count, **_kwargs):
        slots = list(config["generation_slots"])
        return [
            GeneratedContent(
                f"晚点还有安排吗{index}",
                slot_id=slot["slot_id"],
                sequence_index=index + 1,
            )
            for index, slot in enumerate(slots)
        ], count

    return GenerationDependencies(
        normal_generator=normal_generator,
        reply_generator=lambda *_args, **_kwargs: pytest.fail("hard-hourly action must not reply"),
        reply_target_probe=lambda *_args, **_kwargs: pytest.fail("hard-hourly action must not probe reply"),
        reply_messages_fetcher=lambda *_args, **_kwargs: pytest.fail("hard-hourly action must not fetch reply"),
    )


def seed_membership_closed_loop(
    session,
    *,
    add_tenant,
    add_group,
    add_group_task,
):
    add_tenant(session)
    session.add(OperationTarget(
        id=7,
        tenant_id=1,
        target_type="group",
        tg_peer_id="-1007",
        title="测试群目标",
        can_send=False,
        auth_status="只读",
    ))
    add_group(session, account_count=3)
    group = session.get(TgGroup, 7)
    group.can_send = False
    group.slowmode_seconds = None
    for link in session.scalars(select(TgGroupAccount).where(TgGroupAccount.group_id == 7)):
        link.can_send = False
    task = add_group_task(session, {
        "target_operation_target_id": 7,
        "messages_per_round_mode": "manual",
        "messages_per_round": 3,
        "reply_min_per_round": 0,
        "participation_rate": 1,
        "participation_jitter": 0,
        "hard_hourly_target_enabled": True,
        "hourly_min_messages": 3,
        "hard_hourly_strategy": "force_planning",
    })
    return task, group
