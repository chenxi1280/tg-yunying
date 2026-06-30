from __future__ import annotations

import importlib.util
from pathlib import Path
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / ".github/scripts/ai_group_quality_diagnostics.py"
pytestmark = pytest.mark.no_postgres


def load_quality_diagnostics_module():
    spec = importlib.util.spec_from_file_location("ai_group_quality_diagnostics", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ai_group_quality_diagnostics_blocks_stale_online_state():
    module = load_quality_diagnostics_module()

    blockers = module.online_gate_blockers(
        [
            {
                "task_id": "task-ai",
                "name": "郑州楼凤",
                "status": "running",
                "online_summary": {
                    "desired_count": 10,
                    "online_count": 9,
                    "stale_count": 1,
                    "missing_state_count": 0,
                    "blocked_count": 0,
                    "relogin_required_count": 0,
                    "offline_count": 0,
                    "samples": [{"account_id": 7, "bucket": "stale"}],
                },
            }
        ]
    )

    assert blockers == [
        {
            "task_id": "task-ai",
            "name": "郑州楼凤",
            "status": "running",
            "desired_count": 10,
            "online_count": 9,
            "non_online_count": 1,
            "samples": [{"account_id": 7, "bucket": "stale"}],
            "stale_count": 1,
            "missing_state_count": 0,
            "blocked_count": 0,
            "relogin_required_count": 0,
            "offline_count": 0,
        }
    ]


def test_ai_group_quality_diagnostics_accepts_fully_online_state():
    module = load_quality_diagnostics_module()

    blockers = module.online_gate_blockers(
        [
            {
                "task_id": "task-ai",
                "name": "郑州楼凤",
                "status": "running",
                "online_summary": {
                    "desired_count": 10,
                    "online_count": 10,
                    "stale_count": 0,
                    "missing_state_count": 0,
                    "blocked_count": 0,
                    "relogin_required_count": 0,
                    "offline_count": 0,
                },
            }
        ]
    )

    assert blockers == []


def test_ai_group_quality_diagnostics_waits_for_full_active_probe_window():
    module = load_quality_diagnostics_module()

    assert module.ONLINE_SETTLE_SECONDS >= 15 * 60


def test_ai_group_quality_diagnostics_formats_online_failure_row():
    module = load_quality_diagnostics_module()
    now = datetime(2026, 6, 30, 12, 0, 0)
    state = SimpleNamespace(
        account_id=42,
        online_status="offline",
        failure_type="account_unavailable",
        failure_detail="账号没有可用 session，需要重新登录",
        last_probe_at=now - timedelta(minutes=1),
        next_probe_at=now + timedelta(minutes=2),
        stale_after_at=now + timedelta(minutes=9),
    )
    account = SimpleNamespace(display_name="账号42", status="会话过期", health_score=0)

    row = module._online_failure_row(state, account, now)

    assert row == {
        "account_id": 42,
        "display_name": "账号42",
        "account_status": "会话过期",
        "health_score": 0,
        "bucket": "offline",
        "online_status": "offline",
        "failure_type": "account_unavailable",
        "failure_detail": "账号没有可用 session，需要重新登录",
        "last_probe_at": now - timedelta(minutes=1),
        "next_probe_at": now + timedelta(minutes=2),
        "stale_after_at": now + timedelta(minutes=9),
    }


def test_ai_group_quality_diagnostics_blocks_recent_effective_duplicate_text():
    module = load_quality_diagnostics_module()
    actions = [
        SimpleNamespace(id="a1", status="success", payload={"message_text": "嫩是真嫩 就是不知道稳不稳"}),
        SimpleNamespace(id="a2", status="pending", payload={"message_text": "嫩是真嫩 就是不知道稳不稳"}),
        SimpleNamespace(id="a3", status="failed", payload={"message_text": "没发送成功不用阻断"}),
        SimpleNamespace(id="a4", status="skipped", payload={"message_text": "没发送成功不用阻断"}),
    ]

    snapshot = module.recent_action_duplicate_summary(actions)

    assert snapshot["duplicate_blockers"] == [
        {
            "text": "嫩是真嫩 就是不知道稳不稳",
            "effective_count": 2,
            "status_counts": {"pending": 1, "success": 1},
            "action_ids": ["a1", "a2"],
        }
    ]


def test_ai_group_quality_diagnostics_blocks_missing_human_quality_payload():
    module = load_quality_diagnostics_module()
    actions = [
        SimpleNamespace(
            id="a1",
            status="pending",
            account_id=11,
            payload={
                "message_text": "花花老师这个接话还行",
                "account_voice_profile_version": 0,
                "ai_message_memory_id": "",
                "human_quality_decision": "",
                "generation_source": "",
                "act_type": "",
            },
        ),
        SimpleNamespace(
            id="a2",
            status="success",
            account_id=12,
            payload={
                "message_text": "我先看看反馈",
                "account_voice_profile_version": 2,
                "ai_message_memory_id": "memory-2",
                "human_quality_decision": "accepted",
                "generation_source": "ai",
                "act_type": "short_react",
            },
        ),
    ]

    snapshot = module.recent_action_duplicate_summary(actions)

    assert snapshot["quality_payload_blockers"] == [
        {
            "action_id": "a1",
            "account_id": 11,
            "status": "pending",
            "missing_fields": [
                "account_voice_profile_version",
                "ai_message_memory_id",
                "human_quality_decision",
                "generation_source",
                "act_type",
            ],
            "text": "花花老师这个接话还行",
        }
    ]


def test_ai_group_quality_diagnostics_reports_material_trace_samples():
    module = load_quality_diagnostics_module()
    actions = [
        SimpleNamespace(
            id="a1",
            status="success",
            account_id=11,
            scheduled_at=None,
            executed_at=None,
            payload={
                "message_text": "这个表情包挺合适",
                "rule_trace": {
                    "material_intent": "表情包:围观",
                    "material_matched_tags": ["围观", "吃瓜"],
                    "material_candidate_count": 3,
                    "material_ok": True,
                },
            },
        ),
        SimpleNamespace(
            id="a2",
            status="success",
            account_id=12,
            scheduled_at=None,
            executed_at=None,
            payload={"message_text": "普通文本"},
        ),
    ]

    samples = module.material_trace_samples(actions)
    action_samples = module.action_samples(actions)

    assert samples == [
        {
            "action_id": "a1",
            "status": "success",
            "account_id": 11,
            "material_intent": "表情包:围观",
            "material_matched_tags": ["围观", "吃瓜"],
            "material_candidate_count": 3,
            "material_ok": True,
            "text": "这个表情包挺合适",
        }
    ]
    assert action_samples[0]["material_intent"] == "表情包:围观"
    assert action_samples[0]["material_matched_tags"] == ["围观", "吃瓜"]
    assert action_samples[0]["material_candidate_count"] == 3
    assert action_samples[1]["material_intent"] == ""


def test_ai_group_quality_diagnostics_reports_success_only_duplicates_without_blocking():
    module = load_quality_diagnostics_module()
    actions = [
        SimpleNamespace(id="a1", status="success", payload={"message_text": "已发历史重复"}),
        SimpleNamespace(id="a2", status="success", payload={"message_text": "已发历史重复"}),
    ]

    snapshot = module.recent_action_duplicate_summary(actions)

    assert snapshot["sent_duplicate_observations"] == [
        {
            "text": "已发历史重复",
            "sent_count": 2,
            "status_counts": {"success": 2},
            "action_ids": ["a1", "a2"],
        }
    ]
    assert snapshot["duplicate_blockers"] == []


def test_ai_group_quality_diagnostics_ignores_failed_only_duplicate_text():
    module = load_quality_diagnostics_module()
    actions = [
        SimpleNamespace(id="a1", status="failed", payload={"message_text": "失败文本重复"}),
        SimpleNamespace(id="a2", status="skipped", payload={"message_text": "失败文本重复"}),
    ]

    snapshot = module.recent_action_duplicate_summary(actions)

    assert snapshot["repeated_texts"] == [{"text": "失败文本重复", "count": 2}]
    assert snapshot["duplicate_blockers"] == []
