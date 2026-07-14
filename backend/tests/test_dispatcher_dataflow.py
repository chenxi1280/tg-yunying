from types import SimpleNamespace

import pytest

from app.services.task_center import ai_generation_dispatch
from app.services.task_center.payloads import SendMessagePayload


pytestmark = pytest.mark.no_postgres


def _pending_send_payload() -> SendMessagePayload:
    return SendMessagePayload(
        group_id=7,
        target_display="测试群",
        ai_generation_id="batch-1",
        ai_generation_status="pending",
    )


def test_dispatcher_runtime_config_preserves_deferred_generation_slots():
    first = _pending_send_payload()
    first.slot_id = "task-1:cycle:1:turn:1"
    first.turn_index = 1
    first.act_type = "short_react"
    first.account_profile = "青年短句，接话快"
    first.topic_direction = {"title": "郑州楼凤妹子怎么样"}
    first.teacher_target = {"name": "花花老师"}
    second = _pending_send_payload()
    second.slot_id = "task-1:cycle:1:turn:2"
    second.turn_index = 2
    second.act_type = "question"
    second.account_profile = "中年谨慎，常追问"
    second.topic_direction = {"title": "主任最近约新妹子了"}
    second.teacher_target = {"name": "新人榜单妹子"}
    batch = [
        (SimpleNamespace(account_id=11), first),
        (SimpleNamespace(account_id=12), second),
    ]

    config = ai_generation_dispatch._runtime_config(SimpleNamespace(type_config={}), batch)

    assert [slot["topic_direction"]["title"] for slot in config["generation_slots"]] == [
        "郑州楼凤妹子怎么样",
        "主任最近约新妹子了",
    ]
    assert [slot["teacher_target"]["name"] for slot in config["generation_slots"]] == [
        "花花老师",
        "新人榜单妹子",
    ]
    assert [slot["account_profile"] for slot in config["generation_slots"]] == [
        "青年短句，接话快",
        "中年谨慎，常追问",
    ]


def test_dispatcher_runtime_config_does_not_force_mimo_for_hard_hourly_without_model():
    payload = _pending_send_payload()
    payload.hard_hourly_target = True
    batch = [(SimpleNamespace(account_id=11), payload)]

    config = ai_generation_dispatch._runtime_config(SimpleNamespace(type_config={}), batch)

    assert config.get("require_mimo_draft") is None
