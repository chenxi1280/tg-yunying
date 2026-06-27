from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.no_postgres


def test_system_config_exposes_tg_bot_settings_tab() -> None:
    system_view = (PROJECT_ROOT / "frontend/src/app/views/SystemConfigView.tsx").read_text()
    bot_view = PROJECT_ROOT / "frontend/src/app/views/TelegramBotSettingsView.tsx"
    app_shell = (PROJECT_ROOT / "frontend/src/app/AppShell.tsx").read_text()

    assert bot_view.exists()
    assert "key: 'telegram-bot'" in system_view
    assert "label: 'TG Bot 配置'" in system_view
    assert "onSaveTenantBotSettings" in system_view
    assert "onTestTenantBotMessage" in system_view
    assert "loadTelegramBotConfig" in app_shell


def test_tg_bot_settings_view_has_self_service_fields() -> None:
    source = (PROJECT_ROOT / "frontend/src/app/views/TelegramBotSettingsView.tsx").read_text()

    assert "telegram_bot_token" in source
    assert "admin_chat_id" in source
    assert "ai_group_bot_enabled" in source
    assert "notify_ai_failures_enabled" in source
    assert "测试发送" in source
    assert "Bot Token 保存后不会明文回显" in source


def test_task_detail_surfaces_bot_configuration_status() -> None:
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterDetailModal.tsx").read_text()

    assert "telegramBotSettings" in source
    assert "TG bot 未配置" in source
    assert "AI 活群 Bot 设置未启用" in source
