from pathlib import Path

from app.auth import all_permissions, normalize_permissions
from app.permission_middleware import required_permission


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_prd_permission_vocabulary_for_ai_prompt_and_proxy_controls():
    permissions = all_permissions()

    assert "ai.manage" in permissions
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
    assert required_permission("POST", "/api/prompt-templates") == ("prompt_templates.manage",)
    assert required_permission("PATCH", "/api/prompt-templates/12") == ("prompt_templates.manage",)
    assert required_permission("POST", "/api/account-proxies") == ("proxies.manage",)
    assert required_permission("POST", "/api/accounts/12/proxy-binding") == ("proxies.manage",)
    assert required_permission("POST", "/api/proxy-alerts/34/resolve") == ("proxies.manage",)


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


def test_operation_metrics_prd_reports_and_export_routes_use_usage_permission():
    assert required_permission("GET", "/api/operation-metrics/summary") == ("usage.view",)
    assert required_permission("GET", "/api/operation-metrics/reports") == ("usage.view",)
    assert required_permission("POST", "/api/operation-metrics/export") == ("usage.export",)


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
