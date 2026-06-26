import pytest

from app.schemas.task_center import ChannelCommentTaskPreviewRequest, GroupAIChatTaskPreviewRequest
from app.services.task_center import service
from app.services.task_center.ai_generator import AiGenerationUnavailable


def test_group_ai_chat_preview_rejects_candidate_shortfall(monkeypatch):
    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        assert count == 3
        return ["只返回一条预览"], 0

    monkeypatch.setattr(service, "generate_group_messages", fake_generate_group_messages)
    payload = GroupAIChatTaskPreviewRequest(target_input="https://t.me/group", count=3)

    with pytest.raises(AiGenerationUnavailable, match="AI 普通发言候选不足"):
        service.generate_group_ai_chat_preview(None, 1, payload)


def test_channel_comment_preview_rejects_candidate_shortfall(monkeypatch):
    def fake_generate_channel_comments(_session, _tenant_id, _config, *, count, message_content, target_label):
        assert count == 3
        return ["只返回一条评论预览"], 0

    monkeypatch.setattr(service, "generate_channel_comments", fake_generate_channel_comments)
    payload = ChannelCommentTaskPreviewRequest(target_input="https://t.me/channel", message_content="频道原文", count=3)

    with pytest.raises(AiGenerationUnavailable, match="AI 评论候选不足"):
        service.generate_channel_comment_preview(None, 1, payload)
