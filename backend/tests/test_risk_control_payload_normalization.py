from app.models import AccountProxy, AccountStatus, AuditLog, ProxyAlert, SchedulingSetting, TgAccount, TgAccountSecuritySnapshot
from app.schemas.risk_control import (
    RiskControlAccountScoreOut,
    RiskControlSummaryOut,
)
from app.services.account_capacity import AccountCapacityDecision
from app.services.risk_control import _account_score_row, _policy_audit_payload, _proxy_alert_payload, _sort_datetime
from app.timezone import BEIJING_TZ


def test_account_score_payload_coerces_legacy_null_fields():
    account = TgAccount(id=11, tenant_id=1, display_name="legacy", phone_masked="11", status=AccountStatus.ACTIVE.value)
    for field in ["display_name", "phone_masked", "status", "health_score"]:
        setattr(account, field, None)
    snapshot = TgAccountSecuritySnapshot(tenant_id=1, account_id=11)
    for field in ["trusted_session_status", "two_fa_status", "external_authorization_count", "profile_status"]:
        setattr(snapshot, field, None)

    row = _account_score_row(
        account,
        SchedulingSetting(tenant_id=1),
        AccountCapacityDecision(available=True),
        {},
        {},
        "",
        [],
        snapshot,
    )

    payload = RiskControlAccountScoreOut.model_validate(row)
    assert payload.display_name == "账号 #11"
    assert payload.phone_masked == ""
    assert payload.login_status == "unknown"
    assert payload.health_score == 0
    assert payload.trusted_session_status == "unknown"
    assert payload.external_authorization_count == 0


def test_policy_audit_and_proxy_alert_payloads_coerce_legacy_null_fields():
    audit = AuditLog(id=21, tenant_id=1, actor="tester", action="更新风控全局策略", target_type="risk_global_policy", target_id="1")
    for field in ["actor", "action", "target_type", "target_id", "detail", "created_at"]:
        setattr(audit, field, None)
    alert = ProxyAlert(id=31, tenant_id=1, proxy_id=41)
    proxy = AccountProxy(id=41, tenant_id=1, name="legacy-proxy", port=1080)
    for field in ["status", "severity", "alert_type", "reason_code", "affected_account_ids", "suggested_action", "last_seen_at"]:
        setattr(alert, field, None)
    for field in ["name", "protocol", "host", "last_error"]:
        setattr(proxy, field, None)
    alert.proxy = proxy

    summary = {
        "overview": {"current_level": "正常", "level_detail": "正常", "quiet_active": False, "metrics": []},
        "global_policy": {
            "jitter_min_seconds": 0,
            "jitter_max_seconds": 0,
            "batch_interval_seconds": 0,
            "respect_send_window": False,
            "quiet_hours_enabled": False,
            "quiet_start": "",
            "quiet_end": "",
            "quiet_timezone": "Asia/Shanghai",
            "default_max_retries": 0,
            "default_retry_delay_seconds": 0,
            "default_retry_backoff": "none",
            "default_on_account_banned": "skip_account",
            "default_on_api_rate_limit": "wait_and_retry",
            "default_on_content_rejected": "skip_message",
            "default_account_hour_limit": 0,
            "default_account_day_limit": 0,
            "default_account_cooldown_seconds": 0,
            "updated_at": _policy_audit_payload(audit)["occurred_at"],
        },
        "account_scores": [],
        "disposition_queue": [],
        "hit_records": [],
        "policy_audits": [_policy_audit_payload(audit)],
        "proxy_alerts": [_proxy_alert_payload(alert)],
    }

    payload = RiskControlSummaryOut.model_validate(summary)
    assert payload.policy_audits[0].actor == "system"
    assert payload.proxy_alerts[0].name == "proxy_41"


def test_sort_datetime_normalizes_aware_and_naive_values():
    aware = _sort_datetime(_policy_audit_payload(AuditLog(id=1, actor="a", action="b", target_type="c", target_id="d"))["occurred_at"].replace(tzinfo=BEIJING_TZ))
    naive = _sort_datetime(_policy_audit_payload(AuditLog(id=2, actor="a", action="b", target_type="c", target_id="d"))["occurred_at"])

    assert aware.tzinfo is None
    assert naive.tzinfo is None
    sorted([aware, naive], reverse=True)
