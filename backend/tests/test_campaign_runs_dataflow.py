from pathlib import Path
from types import SimpleNamespace

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CAMPAIGN_RUNS = PROJECT_ROOT / "backend/app/services/campaign_runs.py"


def test_mirror_forward_ai_rewrite_failure_is_not_template_success():
    source = CAMPAIGN_RUNS.read_text()
    rewrite_block = source[source.index("def _rewrite_mirror_content"):source.index("\n\n\ndef _auto_queue_draft")]
    mirror_block = source[source.index("def run_mirror_forward_campaign"):source.index("\n\n\ndef process_continuous_campaign")]

    assert "template_fallback" not in rewrite_block
    assert "light_rewrite_message(message.content)" not in rewrite_block
    assert "raise RuntimeError(\"监听转发 AI 润色不可用" in rewrite_block
    assert "代码轻改写" not in mirror_block
    assert "代码轻改写降级" not in mirror_block
    assert "ai_failed_template_fallback" not in mirror_block


def test_mirror_forward_missing_send_account_is_visible_failure():
    source = CAMPAIGN_RUNS.read_text()
    mirror_block = source[source.index("def run_mirror_forward_campaign"):source.index("\n\n\ndef process_continuous_campaign")]
    missing_account_block = mirror_block[mirror_block.index("if not selected_ids:"):mirror_block.index("outbound_content, generation_source")]

    assert 'raise RuntimeError("监听转发目标群没有可用发送账号")' in missing_account_block
    assert "continue" not in missing_account_block


def test_mirror_forward_ai_rewrite_disabled_ai_raises_visible_error(monkeypatch):
    from app.services import campaign_runs

    monkeypatch.setattr(
        campaign_runs,
        "get_tenant_ai_setting",
        lambda _session, _tenant_id: SimpleNamespace(ai_enabled=False),
    )
    monkeypatch.setattr(campaign_runs, "pick_ai_provider", lambda *_args: None)

    with pytest.raises(RuntimeError, match="监听转发 AI 润色不可用"):
        campaign_runs._rewrite_mirror_content(
            None,
            campaign=SimpleNamespace(tenant_id=1, topic="同步源群"),
            target_group=SimpleNamespace(title="目标群", topic_direction="日常聊天"),
            message=SimpleNamespace(sender_name="真人", content="源群今天气氛不错"),
        )
