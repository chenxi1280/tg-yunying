from __future__ import annotations

from app.models import Campaign, PromptTemplate, TgAccount, TgGroup
from app.schemas import GenerateDraftsRequest
from app.services.campaigns import render_prompt


def test_render_prompt_includes_account_order_listener_and_real_context():
    template = PromptTemplate(
        name="多账号对话",
        template_type="多账号对话脚本",
        content="围绕 {{topic}} 生成 {{count}} 条，账号 {{selected_accounts}}，上下文 {{conversation_context}}。",
    )
    campaign = Campaign(id=1, tenant_id=1, group_id=10, title="任务", campaign_type="多账号对话脚本", topic="新品功能")
    group = TgGroup(id=10, tenant_id=1, title="客户群", tg_peer_id="@group", topic_direction="产品讨论")
    accounts = [
        TgAccount(id=101, tenant_id=1, display_name="账号A", username="a", health_score=90),
        TgAccount(id=102, tenant_id=1, display_name="账号B", username="b", health_score=80),
    ]
    listener = TgAccount(id=199, tenant_id=1, display_name="监听号", username="listener", health_score=70)
    payload = GenerateDraftsRequest(
        count=2,
        listener_account_id=199,
        conversation_context=[
            {"sender_name": "真人用户", "content": "这个功能具体怎么用？", "sent_at": "10:01"},
        ],
    )

    prompt = render_prompt(
        template,
        campaign=campaign,
        group=group,
        payload=payload,
        materials=[],
        selected_accounts=accounts,
        listener_account=listener,
    )

    assert "A账号: #101 账号A @a" in prompt
    assert "B账号: #102 账号B @b" in prompt
    assert "监听账号: #199 监听号 @listener" in prompt
    assert "真人用户: 这个功能具体怎么用？" in prompt
    assert "按所选账号顺序从 A 账号开始轮流生成" in prompt
    assert "suggested_account_id 必须优先使用对应账号 id" in prompt
