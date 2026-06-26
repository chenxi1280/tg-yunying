from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres


PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_SHELL = PROJECT_ROOT / "frontend/src/app/AppShell.tsx"
OPERATIONS_TYPES = PROJECT_ROOT / "frontend/src/app/types/operations.ts"
TARGETS_VIEW = PROJECT_ROOT / "frontend/src/app/views/OperationTargetsView.tsx"
TASK_CENTER_VIEW = PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx"


def _read(path: Path) -> str:
    return path.read_text()


def test_single_channel_comment_task_prefill_keeps_comment_identity():
    types_source = _read(OPERATIONS_TYPES)
    app_shell_source = _read(APP_SHELL)
    targets_source = _read(TARGETS_VIEW)
    task_source = _read(TASK_CENTER_VIEW)

    assert "comment?: ChannelMessageComment;" in types_source
    assert "comment?: ChannelMessageComment," in app_shell_source
    assert "comment?: ChannelMessageComment" in targets_source
    assert "onCreateTaskFromTarget('channel_comment', targetDetail.target, message, comment)" in targets_source

    prefill_effect = task_source[
        task_source.index("if (!prefill || appliedPrefillNonce.current === prefill.nonce) return;"):
        task_source.index("}, [form, messages, prefill, schedulingSetting, targets]")
    ]
    assert "if (prefill.comment) {" in prefill_effect
    assert "nextValues.comment_mode = 'reply';" in prefill_effect
    assert "nextValues.reply_to_message_ids = [prefill.comment.comment_message_id];" in prefill_effect
    assert "nextValues.target_comments_per_message = 1;" in prefill_effect
    assert "nextValues.reply_min_per_message = 1;" in prefill_effect

    payload_builder = task_source[
        task_source.index("function channelCommentPayload"):
        task_source.index("\n\n  function parseExcludedSenderInput")
    ]
    assert "comment_mode: values.comment_mode ?? 'comment'," in payload_builder
    assert "reply_to_message_ids: csvNumbers(values.reply_to_message_ids)," in payload_builder
