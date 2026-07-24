from __future__ import annotations

import pytest

from app.services.task_center.conversation_content_quality import (
    CHECK_IN_TEXT,
    evaluate_conversation_content,
    resolve_content_fallback,
)

pytestmark = pytest.mark.no_postgres


def test_quality_rejects_template_shell_and_repeated_opening():
    decision = evaluate_conversation_content(
        content="这个点挺有意思，可以继续聊聊",
        history=["这个点我也留意到了"],
        intent="reaction",
    )
    assert decision.allowed is False
    assert decision.code in {"template_shell", "repeated_opening", "semantic_duplicate"}


def test_direct_generation_failure_uses_audited_check_in_not_old_generic_template():
    resolved = resolve_content_fallback(
        is_reply=False,
        static_fallback_enabled=True,
        last_platform_content_source="ai",
        last_platform_text="正常内容",
    )
    assert resolved.allowed is True
    assert resolved.content == CHECK_IN_TEXT
    assert resolved.content_source == "check_in_fallback"


def test_reply_generation_failure_never_degrades_to_unlinked_check_in():
    resolved = resolve_content_fallback(is_reply=True, static_fallback_enabled=True)
    assert resolved.allowed is False
    assert resolved.code == "reply_cannot_use_check_in"


def test_check_in_quota_and_repeat_guards():
    assert resolve_content_fallback(
        is_reply=False,
        static_fallback_enabled=True,
        last_platform_content_source="check_in_fallback",
    ).code == "check_in_repeat"
    assert resolve_content_fallback(
        is_reply=False,
        static_fallback_enabled=True,
        session_check_in_count_30m=3,
    ).code == "check_in_quota_exceeded"
