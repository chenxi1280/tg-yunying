from pathlib import Path

import pytest

from app.auth import ROLE_TEMPLATE_PERMISSIONS, all_permissions, normalize_permissions
from app.permission_middleware import permission_check_result, required_permission


PROJECT_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.no_postgres


def test_prd_permission_vocabulary_for_ai_prompt_and_proxy_controls():
    permissions = all_permissions()

    assert "ai.manage" in permissions
    assert "ai_voice_profiles.manage" in permissions
    assert "prompt_templates.manage" in permissions
    assert "proxies.manage" in permissions
    assert "usage.export" in permissions
    assert "system.secrets_manage" not in permissions
    assert "accounts.proxy_bind" not in permissions

    assert normalize_permissions(["system.secrets_manage", "accounts.proxy_bind", "message_sending.create"]) == [
        "ai.manage",
        "proxies.manage",
        "message_sending.manage",
    ]
    assert required_permission("POST", "/api/ai-providers") == ("ai.manage",)
    assert required_permission("PATCH", "/api/tenant-ai-settings") == ("ai.manage",)
    assert required_permission("GET", "/api/ai-account-voice-profiles") == ("system.view", "ai_voice_profiles.manage")
    assert required_permission("GET", "/api/ai-account-voice-profiles/12/versions") == ("system.view", "ai_voice_profiles.manage")
    assert required_permission("GET", "/api/ai-account-voice-profiles/12/audits") == ("system.view", "ai_voice_profiles.manage")
    assert required_permission("PATCH", "/api/ai-account-voice-profiles/12") == ("ai_voice_profiles.manage",)
    assert required_permission("POST", "/api/ai-account-voice-profiles/12/rebuild") == ("ai_voice_profiles.manage",)
    assert required_permission("POST", "/api/ai-account-voice-profiles/12/rollback") == ("ai_voice_profiles.manage",)
    assert required_permission("POST", "/api/ai-account-voice-profiles/batch-rebuild") == ("ai_voice_profiles.manage",)
    assert required_permission("POST", "/api/ai-account-voice-profiles/batch-status") == ("ai_voice_profiles.manage",)
    assert required_permission("POST", "/api/prompt-templates") == ("prompt_templates.manage",)
    assert required_permission("PATCH", "/api/prompt-templates/12") == ("prompt_templates.manage",)
    assert required_permission("POST", "/api/account-proxies") == ("proxies.manage",)
    assert required_permission("POST", "/api/accounts/12/proxy-binding") == ("proxies.manage",)
    assert required_permission("POST", "/api/proxy-alerts/34/resolve") == ("proxies.manage",)


def test_operator_template_can_open_and_manage_ai_voice_profiles():
    operator_permissions = ROLE_TEMPLATE_PERMISSIONS["运营管理员"]

    assert "system.view" in operator_permissions
    assert "ai_voice_profiles.manage" in operator_permissions


def test_operation_issue_read_routes_are_view_only_and_status_actions_require_manage():
    assert required_permission("GET", "/api/operation-issues") == ("overview.view", "operation_issues.manage")
    assert required_permission("GET", "/api/operation-issues/issue-1") == ("overview.view", "operation_issues.manage")
    for action in ["claim", "acknowledge", "resolve", "ignore"]:
        assert required_permission("POST", f"/api/operation-issues/issue-1/{action}") == ("operation_issues.manage",)


def test_sensitive_read_routes_have_explicit_least_privilege_rules():
    assert required_permission("GET", "/api/config/runtime") == ("system.view",)
    assert required_permission("GET", "/api/account-clone-plans") == ("accounts.clone",)
    assert required_permission("GET", "/api/account-clone-plans/12") == ("accounts.clone",)
    assert required_permission("GET", "/api/verification-tasks") == ("accounts.sync",)
    assert required_permission("GET", "/api/tg-accounts/12/verification-tasks") == ("accounts.sync",)
    assert required_permission("GET", "/api/groups/34/verification-tasks") == ("accounts.sync",)
    assert required_permission("GET", "/api/channel-comments") == ("targets.view",)
    assert required_permission("GET", "/api/rules/relay-attribution/report") == ("rules.view",)
    assert required_permission("PATCH", "/api/tg-accounts/12/identity") == ("accounts.pool_manage",)
    assert required_permission("POST", "/api/tg-accounts/12/pending-execution/recheck") == ("accounts.sync",)


def test_auth_change_password_route_exists_for_frontend_self_service_flow():
    auth_router = (PROJECT_ROOT / "backend/app/api/routers/auth.py").read_text()
    auth_actions = (PROJECT_ROOT / "frontend/src/app/context/authActions.ts").read_text()

    assert "api<CurrentUser>('/auth/change-password'" in auth_actions
    assert '@router.post("/api/auth/change-password"' in auth_router
    assert "AuthChangePasswordRequest" in auth_router
    assert "verify_password" in auth_router


def test_operation_metrics_prd_reports_and_export_routes_use_usage_permission():
    assert required_permission("GET", "/api/operation-metrics/summary") == ("usage.view",)
    assert required_permission("GET", "/api/operation-metrics/reports") == ("usage.view",)
    assert required_permission("POST", "/api/operation-metrics/export") == ("usage.export",)


def test_system_and_legacy_task_write_routes_have_backend_permission_rules():
    assert required_permission("PATCH", "/api/tenant-group-rescue-settings") == ("system.manage",)
    assert required_permission("GET", "/api/tenant-notification-settings") == ("system.view",)
    assert required_permission("GET", "/api/tenant-group-rescue-settings") == ("system.view",)
    assert required_permission("POST", "/api/telegram-bot/tasks/group-ai-chat/settings") == ("system.manage",)
    assert required_permission("POST", "/api/telegram-bot/update") == ("system.manage",)
    assert required_permission("POST", "/api/telegram-bot/webhook/1/secret") is None
    assert required_permission("GET", "/api/operation-tasks") == ("tasks.view",)
    assert required_permission("GET", "/api/operation-task-attempts") == ("tasks.view",)
    assert required_permission("GET", "/api/manual-operation-records") == ("tasks.view",)
    assert required_permission("POST", "/api/operation-tasks") == ("tasks.manage",)
    assert required_permission("POST", "/api/operation-tasks/123/dispatch") == ("tasks.manage",)
    assert required_permission("POST", "/api/operation-tasks/123/retry") == ("tasks.manage",)
    assert required_permission("POST", "/api/operation-tasks/123/cancel") == ("tasks.manage",)
    assert required_permission("GET", "/api/review-queue") == ("tasks.view",)
    assert required_permission("POST", "/api/review/abc/approve") == ("tasks.manage",)
    assert required_permission("POST", "/api/review/abc/reject") == ("tasks.manage",)
    assert required_permission("POST", "/api/tasks/search-join-group") == ("tasks.manage", "tasks.create.search_join_group")
    assert required_permission("POST", "/api/tasks/search-join-group/create-and-start") == (
        "tasks.manage",
        "tasks.create.search_join_group",
    )
    assert permission_check_result(("tasks.manage", "tasks.create.search_join_group"), {"tasks.manage"}) == [
        "tasks.create.search_join_group"
    ]
    assert permission_check_result(
        ("tasks.manage", "tasks.create.search_join_group"),
        {"tasks.manage", "tasks.create.search_join_group"},
    ) == []
    assert permission_check_result(("overview.view", "operation_issues.manage"), {"overview.view"}) == []


def test_legacy_campaign_routes_use_message_sending_permissions():
    assert required_permission("GET", "/api/campaigns") == ("message_sending.view",)
    assert required_permission("GET", "/api/campaigns/123/detail") == ("message_sending.view",)
    assert required_permission("GET", "/api/ai-drafts") == ("message_sending.view",)
    assert required_permission("POST", "/api/campaigns") == ("message_sending.manage",)
    assert required_permission("POST", "/api/campaigns/recommend-accounts") == ("message_sending.manage",)
    assert required_permission("POST", "/api/campaigns/123/generate-drafts") == ("message_sending.manage",)
    assert required_permission("POST", "/api/campaigns/123/approve-all") == ("message_sending.manage",)
    assert required_permission("POST", "/api/campaigns/123/cancel") == ("message_sending.manage",)
    assert required_permission("POST", "/api/ai-drafts/123/approve") == ("message_sending.manage",)
    assert required_permission("PATCH", "/api/ai-drafts/123") == ("message_sending.manage",)
    assert required_permission("POST", "/api/ai-drafts/123/reject") == ("message_sending.manage",)


def test_target_profile_routes_use_top_level_permissions_and_old_routes_are_removed():
    permissions = all_permissions()
    operations_router = (PROJECT_ROOT / "backend/app/api/routers/operations.py").read_text()
    operations_center_router = (PROJECT_ROOT / "backend/app/api/routers/operations_center.py").read_text()

    assert "target_profile.view" in permissions
    assert "target_profile.manage" in permissions
    assert "target_learning.view" not in permissions
    assert "target_learning.manage" not in permissions
    assert "target_learning.rebuild" not in permissions
    assert required_permission("GET", "/api/target-profile") == ("target_profile.view",)
    assert required_permission("GET", "/api/target-profile/source-candidates") == ("target_profile.view",)
    assert required_permission("POST", "/api/target-profile/rebuild") == ("target_profile.manage",)
    assert required_permission("PATCH", "/api/target-profile/quality-rules") == ("target_profile.manage",)
    assert "learning-profile" not in operations_router
    assert "learning-samples" not in operations_router
    assert "learning-versions" not in operations_router
    assert "learning-profile" not in operations_center_router
    assert "learning-samples" not in operations_center_router
