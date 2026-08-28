import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.services.task_center.ai_group_prompt import sanitize_group_message_text
from app.integrations.telegram.gateway import TelethonTelegramGateway
from app.integrations.telegram.contracts import DeveloperAppCredentials


pytestmark = pytest.mark.no_postgres


def test_sanitize_group_message_text_strips_urls_and_mentions():
    # 1. 纯口语正常短句应完整保留
    text1 = "这家水汇环境怎么样，有老哥去过吗？"
    assert sanitize_group_message_text(text1) == "这家水汇环境怎么样，有老哥去过吗？"

    # 2. 剥离 http/https/t.me 链接
    text2 = "老哥们推荐一个好地方 https://t.me/test_sample_channel 看看"
    assert sanitize_group_message_text(text2) == "老哥们推荐一个好地方 看看"

    # 3. 剥离 @username 提及
    text3 = "联系 @messi123 咨询详情"
    assert sanitize_group_message_text(text3) == "联系 咨询详情"

    # 4. 剥离 Markdown 链接 [text](url) -> text
    text4 = "点击 [查看详情](https://t.me/some_channel) 了解更多"
    assert sanitize_group_message_text(text4) == "点击 查看详情 了解更多"

    # 5. 剥离裸域名
    text5 = "去 www.example.com 或者 test.xyz 看看吧"
    assert sanitize_group_message_text(text5) == "去 或者 看看吧"

    # 6. 剥离外层多余引号与空格
    text6 = "  “今天天气还挺不错”  "
    assert sanitize_group_message_text(text6) == "今天天气还挺不错"

    # 7. 不得遗漏未枚举的有效顶级域名
    assert sanitize_group_message_text("去 evil.ai 或 telegram.dog/demo 看看") == "去 或 看看"

@pytest.mark.anyio
async def test_gateway_send_async_passes_link_preview_false_and_typing():
    gateway = TelethonTelegramGateway()
    mock_client = AsyncMock()
    mock_client.is_user_authorized.return_value = True

    mock_msg = MagicMock()
    mock_msg.id = 12345
    mock_client.send_message.return_value = mock_msg

    with patch.object(gateway, "_get_or_create_client", return_value=mock_client), \
         patch("app.integrations.telegram.gateway.decrypt_session", return_value="fake_session"), \
         patch("app.integrations.telegram.gateway.resolve_telethon_target", return_value="fake_target"), \
         patch("asyncio.sleep", new_callable=AsyncMock):

        credentials = DeveloperAppCredentials(
            app_id=1,
            api_id=12345,
            api_hash="fake_hash",
            credentials_version=1,
            app_name="test_app"
        )

        result = await gateway._send_async(
            session_ciphertext="fake_cipher",
            peer_id="-100123456789",
            content="测试纯文本发言",
            segments=None,
            credentials=credentials
        )

        assert result.ok is True
        assert result.remote_message_id == "12345"

        # 验证 send_message 明确传入了 link_preview=False
        mock_client.send_message.assert_called_once()
        _, kwargs = mock_client.send_message.call_args
        assert kwargs.get("link_preview") is False


@pytest.mark.anyio
async def test_gateway_typing_failure_stays_before_send_boundary():
    gateway = TelethonTelegramGateway()
    mock_client = AsyncMock()
    mock_client.is_user_authorized.return_value = True
    mock_client.side_effect = RuntimeError("typing request failed")

    with patch.object(gateway, "_get_or_create_client", return_value=mock_client), \
         patch("app.integrations.telegram.gateway.decrypt_session", return_value="fake_session"), \
         patch("app.integrations.telegram.gateway.resolve_telethon_target", return_value="fake_target"):
        result = await gateway._send_async(
            session_ciphertext="fake_cipher",
            peer_id="-100123456789",
            content="测试纯文本发言",
            segments=None,
            credentials=DeveloperAppCredentials(
                app_id=1,
                api_id=12345,
                api_hash="fake_hash",
                credentials_version=1,
                app_name="test_app",
            ),
        )

    assert result.ok is False
    assert result.remote_mutation_started is False
    mock_client.send_message.assert_not_called()
