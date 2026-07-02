from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
import pytest

from app.database import Base
from app.models import (
    AccountStatus,
    GroupAuthStatus,
    TelegramDeveloperApp,
    Tenant,
    TgAccount,
    TgAccountAuthorization,
    TgAccountAuthorizationSnapshot,
    TgAccountDeviceCleanupPrecheck,
    TgAccountSecurityBatch,
    TgAccountSecurityBatchItem,
    TgGroup,
    TgGroupAccount,
    TgVerificationCode,
    MessageTask,
    Task,
    Action,
    ExecutionAttempt,
    AccountRuntimeSummary,
)
from app.services.accounts import account_execution_records, recheck_account_pending_execution
import app.services.accounts as accounts_service
from app.services.account_authorization_read_model import authorization_summary_for_account, list_account_authorizations
import app.services.account_security.service as account_security_service
from app.security import encrypt_secret
from app.services.account_security.device_classification import classify_account_authorization_snapshots, cleanup_candidate_authorization_snapshots
from app.services.account_security.service import create_device_cleanup_precheck, cleanup_devices_from_precheck
from app.services.account_search import filter_accounts_by_search
from app.services.task_center.account_pool import select_task_accounts
from app.integrations.telegram.mock import TelegramGateway
from app.api.routers import account_security as account_security_router

pytestmark = pytest.mark.no_postgres
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def test_authorization_summary_derives_three_slot_states_and_all_down() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        account = TgAccount(id=1, tenant_id=1, display_name="掉线账号", phone_masked="1", status=AccountStatus.ACTIVE.value)
        session.add(account)
        session.flush()
        session.add_all(
            [
                TgAccountAuthorization(tenant_id=1, account_id=1, role="primary", status="active", health_status="expired", session_ciphertext="primary"),
                TgAccountAuthorization(tenant_id=1, account_id=1, role="standby_1", status="active", health_status="failed", session_ciphertext="standby1"),
                TgAccountAuthorization(tenant_id=1, account_id=1, role="standby_2", status="active", health_status="failed", session_ciphertext="standby2"),
            ]
        )
        session.commit()

        summary = authorization_summary_for_account(session, account)

        assert summary["slot_statuses"] == {
            "primary": "down",
            "standby_1": "down",
            "standby_2": "down",
        }
        assert summary["aggregate_status"] == "previously_logged_in_all_down"
        assert summary["healthy_slot_count"] == 0
        assert summary["can_rescue"] is False


def test_authorization_assets_expose_api_id_snapshot_and_derived_status() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TelegramDeveloperApp(id=10, app_name="Primary App", api_id=10001, api_hash_ciphertext="secret"))
        account = TgAccount(id=2, tenant_id=1, display_name="授权账号", phone_masked="2", status=AccountStatus.ACTIVE.value)
        session.add(account)
        session.flush()
        session.add(
            TgAccountAuthorization(
                tenant_id=1,
                account_id=2,
                role="primary",
                developer_app_id=10,
                status="active",
                health_status="healthy",
                session_ciphertext="primary",
                is_current=True,
            )
        )
        session.commit()

        rows = list_account_authorizations(session, 2)

        assert rows[0]["developer_app_api_id"] == 10001
        assert rows[0]["derived_status"] == "healthy"


def test_device_classification_uses_remote_api_id_against_three_slots() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TelegramDeveloperApp(id=1, app_name="Primary", api_id=111, api_hash_ciphertext="secret"),
                TelegramDeveloperApp(id=2, app_name="Standby", api_id=222, api_hash_ciphertext="secret"),
            ]
        )
        account = TgAccount(id=3, tenant_id=1, display_name="设备账号", phone_masked="3", status=AccountStatus.ACTIVE.value)
        session.add(account)
        session.flush()
        session.add_all(
            [
                TgAccountAuthorization(tenant_id=1, account_id=3, role="primary", developer_app_id=1, session_ciphertext="p", status="active"),
                TgAccountAuthorization(tenant_id=1, account_id=3, role="standby_1", developer_app_id=2, session_ciphertext="s", status="standby"),
                TgAccountAuthorizationSnapshot(tenant_id=1, account_id=3, api_id=111, app_name="Primary", device_model="平台主控"),
                TgAccountAuthorizationSnapshot(tenant_id=1, account_id=3, api_id=2040, app_name="Telegram Desktop", device_model="Desktop"),
                TgAccountAuthorizationSnapshot(tenant_id=1, account_id=3, api_id=0, app_name="Unknown", device_model="Unknown"),
            ]
        )
        session.commit()

        classified = classify_account_authorization_snapshots(session, account.id)

        assert [item["classification"] for item in classified] == ["platform_app", "official_anchor", "unknown"]
        assert classified[0]["matched_roles"] == ["primary"]
        assert classified[1]["cleanup_eligible"] is False
        assert classified[2]["cleanup_eligible"] is False


def test_device_cleanup_candidates_use_api_id_not_telegram_client_names() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TelegramDeveloperApp(id=1, app_name="Primary", api_id=12345, api_hash_ciphertext="secret"))
        account = TgAccount(
            id=6,
            tenant_id=1,
            display_name="清理账号",
            phone_masked="6",
            developer_app_id=1,
            status=AccountStatus.ACTIVE.value,
        )
        session.add(account)
        session.flush()
        session.add_all(
            [
                TgAccountAuthorizationSnapshot(tenant_id=1, account_id=6, api_id=12345, app_name="平台应用", device_model="平台"),
                TgAccountAuthorizationSnapshot(tenant_id=1, account_id=6, api_id=2040, app_name="Telegram Desktop", device_model="Desktop"),
                TgAccountAuthorizationSnapshot(tenant_id=1, account_id=6, api_id=999999, app_name="Legacy Client", device_model="Unknown"),
                TgAccountAuthorizationSnapshot(tenant_id=1, account_id=6, api_id=0, app_name="Unknown", device_model="Unknown"),
            ]
        )
        session.commit()

        candidates = cleanup_candidate_authorization_snapshots(session, account)

        assert [item.app_name for item in candidates] == ["Legacy Client"]


def test_device_cleanup_confirm_consumes_precheck_snapshot_without_expanding(monkeypatch) -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TelegramDeveloperApp(id=1, app_name="Primary", api_id=12345, api_hash_ciphertext=encrypt_secret("hash")))
        account = TgAccount(
            id=9,
            tenant_id=1,
            display_name="快照账号",
            phone_masked="9",
            developer_app_id=1,
            status=AccountStatus.ACTIVE.value,
            session_ciphertext="session",
        )
        session.add(account)
        session.flush()
        session.add_all(
            [
                TgAccountAuthorizationSnapshot(
                    tenant_id=1,
                    account_id=9,
                    api_id=12345,
                    app_name="平台应用",
                    authorization_hash_ciphertext=encrypt_secret("platform-hash"),
                ),
                TgAccountAuthorizationSnapshot(
                    tenant_id=1,
                    account_id=9,
                    api_id=999999,
                    app_name="Legacy Client",
                    authorization_hash_ciphertext=encrypt_secret("legacy-hash"),
                ),
            ]
        )
        session.commit()

        precheck = create_device_cleanup_precheck(session, 1, 9, "tester")
        session.add(
            TgAccountAuthorizationSnapshot(
                tenant_id=1,
                account_id=9,
                api_id=999999,
                app_name="Late Client",
                authorization_hash_ciphertext=encrypt_secret("late-hash"),
            )
        )
        session.commit()
        cleaned_hashes: list[str] = []
        monkeypatch.setattr(account_security_service, "credentials_for_account", lambda *_args, **_kwargs: SimpleNamespace())
        monkeypatch.setattr(
            account_security_service.gateway,
            "cleanup_authorization",
            lambda _session_ciphertext, authorization_hash, _credentials: cleaned_hashes.append(authorization_hash) or SimpleNamespace(ok=True, detail="", failure_type=""),
        )

        result = cleanup_devices_from_precheck(session, 1, 9, precheck["precheck_id"], "tester")

        assert precheck["cleanup_count"] == 1
        assert result["cleaned_count"] == 1
        assert cleaned_hashes == ["legacy-hash"]


def test_device_cleanup_precheck_returns_kept_cleanup_and_unknown_device_details() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TelegramDeveloperApp(id=1, app_name="Primary", api_id=12345, api_hash_ciphertext=encrypt_secret("hash")))
        account = TgAccount(
            id=26,
            tenant_id=1,
            display_name="明细账号",
            phone_masked="26",
            status=AccountStatus.ACTIVE.value,
            session_ciphertext="session",
        )
        session.add(account)
        session.flush()
        session.add(
            TgAccountAuthorization(
                tenant_id=1,
                account_id=26,
                role="primary",
                developer_app_id=1,
                developer_app_api_id_snapshot=12345,
                status="active",
                session_ciphertext="primary",
                telegram_authorization_hash_ciphertext=encrypt_secret("platform-hash"),
            )
        )
        session.add_all(
            [
                TgAccountAuthorizationSnapshot(
                    tenant_id=1,
                    account_id=26,
                    api_id=12345,
                    app_name="平台应用",
                    device_model="平台主控",
                    authorization_hash_ciphertext=encrypt_secret("platform-hash"),
                ),
                TgAccountAuthorizationSnapshot(
                    tenant_id=1,
                    account_id=26,
                    api_id=2040,
                    app_name="Telegram Desktop",
                    device_model="Desktop",
                    authorization_hash_ciphertext=encrypt_secret("desktop-hash"),
                ),
                TgAccountAuthorizationSnapshot(
                    tenant_id=1,
                    account_id=26,
                    api_id=999999,
                    app_name="Legacy Client",
                    device_model="Unknown",
                    authorization_hash_ciphertext=encrypt_secret("legacy-hash"),
                ),
                TgAccountAuthorizationSnapshot(
                    tenant_id=1,
                    account_id=26,
                    api_id=0,
                    app_name="Unknown",
                    device_model="Unknown",
                    authorization_hash_ciphertext=encrypt_secret("unknown-hash"),
                ),
            ]
        )
        session.commit()

        precheck = create_device_cleanup_precheck(session, 1, 26, "tester")

        assert [item["classification"] for item in precheck["kept_devices"]] == ["platform_app", "official_anchor"]
        assert [item["classification"] for item in precheck["cleanup_devices"]] == ["non_platform_app"]
        assert [item["classification"] for item in precheck["unknown_devices"]] == ["unknown"]
        assert precheck["cleanup_devices"][0]["app_name"] == "Legacy Client"


def test_device_cleanup_precheck_blocks_when_platform_slot_hash_unconfirmed() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TelegramDeveloperApp(id=1, app_name="Primary", api_id=12345, api_hash_ciphertext=encrypt_secret("hash")))
        account = TgAccount(
            id=23,
            tenant_id=1,
            display_name="缺 hash 账号",
            phone_masked="23",
            status=AccountStatus.ACTIVE.value,
            session_ciphertext="session",
        )
        session.add(account)
        session.flush()
        session.add(
            TgAccountAuthorization(
                tenant_id=1,
                account_id=23,
                role="primary",
                developer_app_id=1,
                status="active",
                session_ciphertext="primary",
            )
        )
        session.add_all(
            [
                TgAccountAuthorizationSnapshot(
                    tenant_id=1,
                    account_id=23,
                    api_id=12345,
                    app_name="平台应用",
                    authorization_hash_ciphertext=encrypt_secret("platform-hash"),
                ),
                TgAccountAuthorizationSnapshot(
                    tenant_id=1,
                    account_id=23,
                    api_id=2040,
                    app_name="Telegram Desktop",
                    authorization_hash_ciphertext=encrypt_secret("desktop-hash"),
                ),
            ]
        )
        session.commit()

        with pytest.raises(ValueError, match="平台授权设备 hash 未确认"):
            create_device_cleanup_precheck(session, 1, 23, "tester")


def test_security_batch_cleanup_consumes_stored_precheck_snapshot(monkeypatch) -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        account = TgAccount(id=16, tenant_id=1, display_name="批次清理账号", phone_masked="16", status=AccountStatus.ACTIVE.value, session_ciphertext="session")
        session.add(account)
        session.add_all(
            [
                TgAccountAuthorizationSnapshot(
                    id=20,
                    tenant_id=1,
                    account_id=16,
                    authorization_hash_ciphertext=encrypt_secret("old-device"),
                    api_id=999,
                    app_name="Other",
                    is_current_session=False,
                ),
                TgAccountAuthorizationSnapshot(
                    id=21,
                    tenant_id=1,
                    account_id=16,
                    authorization_hash_ciphertext=encrypt_secret("late-device"),
                    api_id=999,
                    app_name="Other",
                    is_current_session=False,
                ),
            ]
        )
        session.add(
            TgAccountDeviceCleanupPrecheck(
                precheck_id="device_cleanup_batch",
                tenant_id=1,
                account_id=16,
                cleanup_authorization_hashes='["old-device"]',
                cleanup_count=1,
                expires_at=account_security_service._now() + timedelta(minutes=15),
            )
        )
        session.add(TgAccountSecurityBatch(id=1, tenant_id=1, action_types='["cleanup_devices"]'))
        item = TgAccountSecurityBatchItem(id=1, batch_id=1, tenant_id=1, account_id=16, device_cleanup_precheck_id="device_cleanup_batch")
        session.add(item)
        session.commit()
        cleaned_hashes: list[str] = []
        monkeypatch.setattr(
            account_security_service.gateway,
            "cleanup_authorization",
            lambda _session_ciphertext, authorization_hash, _credentials: cleaned_hashes.append(authorization_hash) or SimpleNamespace(ok=True, detail="", failure_type=""),
        )

        failures = account_security_service._execute_cleanup(session, account, item, SimpleNamespace())

        assert failures == []
        assert cleaned_hashes == ["old-device"]


def test_code_receiver_identity_is_excluded_from_task_account_selection() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        normal = TgAccount(id=4, tenant_id=1, display_name="普通账号", phone_masked="4", status=AccountStatus.ACTIVE.value, health_score=95)
        code_receiver = TgAccount(id=5, tenant_id=1, display_name="接码账号", phone_masked="5", status=AccountStatus.ACTIVE.value, health_score=99)
        code_receiver.account_identity = "code_receiver"
        session.add_all([normal, code_receiver])
        session.commit()

        selected = select_task_accounts(session, 1, {"max_concurrent": 5}, limit=5)

        assert [account.id for account in selected] == [4]


def test_account_center_routes_expose_code_receiver_and_identity_contracts() -> None:
    account_pools_router = (PROJECT_ROOT / "app/api/routers/account_pools.py").read_text()
    accounts_router = (PROJECT_ROOT / "app/api/routers/accounts.py").read_text()
    account_security_router = (PROJECT_ROOT / "app/api/routers/account_security.py").read_text()
    schemas = (PROJECT_ROOT / "app/schemas/accounts.py").read_text()
    security_schemas = (PROJECT_ROOT / "app/schemas/account_security.py").read_text()

    assert '"/api/account-pools/code-receiver"' in account_pools_router
    assert "ensure_code_receiver_account_pool" in account_pools_router
    assert '"/api/tg-accounts/{account_id}/devices/cleanup/precheck"' in account_security_router
    assert '"/api/tg-accounts/{account_id}/devices/cleanup"' in account_security_router
    assert '"/api/tg-accounts/{account_id}/identity"' in accounts_router
    assert '"/api/tg-accounts/{account_id}/pending-execution/recheck"' in accounts_router
    assert '"/api/tg-accounts/{account_id}/execution-records"' in accounts_router
    assert "set_account_identity" in accounts_router
    assert "recheck_account_pending_execution" in accounts_router
    assert "account_execution_records" in accounts_router
    assert "class AccountIdentityUpdate" in schemas
    assert 'Field(pattern="^(normal|code_receiver)$")' in schemas
    assert "slot_statuses: dict[str, str]" in schemas
    assert "aggregate_status: str" in schemas
    assert "healthy_slot_count: int" in schemas
    assert "can_rescue: bool" in schemas
    assert "developer_app_api_id: int" in schemas
    assert "derived_status: str" in schemas
    runtime_schema = (PROJECT_ROOT / "app/schemas/runtime_summary.py").read_text()
    assert "capacity_limit: int" in runtime_schema
    assert "capacity_used: int" in runtime_schema
    assert "capacity_explanation: str" in runtime_schema
    assert "class AccountExecutionRecordOut" in schemas
    assert "class AccountPendingExecutionRecheckOut" in schemas
    assert "class DeviceCleanupPrecheckOut" in security_schemas
    assert "device_cleanup_precheck_id: str" in security_schemas
    assert "kept_devices: list[dict[str, object]]" in security_schemas
    assert "cleanup_devices: list[dict[str, object]]" in security_schemas
    assert "unknown_devices: list[dict[str, object]]" in security_schemas
    assert "classification: str" in security_schemas
    assert "matched_roles: list[str]" in security_schemas
    assert "cleanup_eligible: bool" in security_schemas


def test_frontend_account_types_include_identity_and_pool_purpose() -> None:
    types = (PROJECT_ROOT.parent / "frontend/src/app/types/accounts.ts").read_text()
    account_type = types[types.index("export type Account = {"):types.index("export type AccountAuthorizationAsset")]
    pool_type = types[types.index("export type AccountPool = {"):types.index("export type DeveloperApp")]

    assert "account_identity: 'normal' | 'code_receiver' | string;" in account_type
    assert "pool_purpose: 'normal' | 'code_receiver' | string;" in pool_type
    assert "is_system: boolean;" in pool_type
    assert "system_key: string;" in pool_type

    auth_types = (PROJECT_ROOT.parent / "frontend/src/app/types/accountAuth.ts").read_text()
    assert "slot_statuses: Record<string, string>;" in auth_types
    assert "aggregate_status: string;" in auth_types
    assert "healthy_slot_count: number;" in auth_types
    assert "can_rescue: boolean;" in auth_types

    auth_asset_type = types[types.index("export type AccountAuthorizationAsset = {"):types.index("export type AccountAvailabilitySummary")]
    availability_type = types[types.index("export type AccountAvailabilitySummary = {"):types.index("export type AccountPool")]
    assert "developer_app_api_id: number;" in auth_asset_type
    assert "derived_status: string;" in auth_asset_type
    assert "capacity_limit: number;" in availability_type
    assert "capacity_used: number;" in availability_type
    assert "capacity_explanation: string;" in availability_type
    assert "export type AccountExecutionRecord = {" in types
    assert "action_label: string;" in types
    assert "status_label: string;" in types
    assert "export type AccountPendingExecutionRecheck = {" in types
    assert "requeued_count: number;" in types
    assert "blockers: { action_id: string; reason: string }[];" in types
    assert "device_cleanup_precheck_id: string;" in types
    authorization_snapshot_type = types[types.index("export type AccountAuthorizationSnapshot = {"):types.index("export type AccountSecuritySnapshot")]
    assert "classification: string;" in authorization_snapshot_type
    assert "matched_roles: string[];" in authorization_snapshot_type
    assert "cleanup_eligible: boolean;" in authorization_snapshot_type
    app_modals = (PROJECT_ROOT.parent / "frontend/src/app/AppModals.tsx").read_text()
    assert "accounts.security.session_manage" in app_modals


def test_account_detail_uses_execution_records_recheck_and_capacity_explanation_contracts() -> None:
    frontend_root = PROJECT_ROOT.parent / "frontend/src/app/views"
    modal = (frontend_root / "AccountModals.tsx").read_text()
    panel = (frontend_root / "AccountExecutionRecordsPanel.tsx").read_text()
    response_permissions = (PROJECT_ROOT / "app/api/response_permissions.py").read_text()

    assert "AccountExecutionRecordsPanel" in modal
    assert "/execution-records" in panel
    assert "/pending-execution/recheck" in panel
    assert "capacity_explanation" in modal
    assert "capacity_explanation" in response_permissions


def test_account_security_device_table_exposes_api_id_classification_and_cleanup_fields() -> None:
    modal = (PROJECT_ROOT.parent / "frontend/src/app/views/AccountModals.tsx").read_text()

    assert "authorization.api_id" in modal
    assert "authorization.classification" in modal
    assert "authorization.matched_roles" in modal
    assert "authorization.cleanup_eligible" in modal


def test_frontend_authorization_assets_exposes_rescue_refresh_contracts() -> None:
    panel = (PROJECT_ROOT.parent / "frontend/src/app/views/AccountAuthorizationAssetsPanel.tsx").read_text()

    assert "/authorizations/${authorizationId}/activate" in panel
    assert "/authorizations/${authorizationId}/refresh" in panel
    assert "/authorizations/self-heal" in panel
    assert "激活恢复" in panel
    assert "刷新槽位" in panel
    assert "自愈恢复" in panel
    assert "developer_app_api_id" in panel
    assert "switch-primary" not in panel


def test_standby_session_batches_use_session_manage_permission() -> None:
    router = (PROJECT_ROOT / "app/api/routers/account_security.py").read_text()
    middleware = (PROJECT_ROOT / "app/permission_middleware.py").read_text()
    auth = (PROJECT_ROOT / "app/auth.py").read_text()

    assert "accounts.security.session_manage required" in router
    assert "accounts.security.session_manage" in middleware
    assert '"accounts.security.session_manage"' in auth


def test_standby_session_batch_retry_requires_session_manage_permission() -> None:
    router_source = (PROJECT_ROOT / "app/api/routers/account_security.py").read_text()
    assert "_require_retry_batch_permissions(session, current_user.tenant_id or 1, batch_id, current_user)" in router_source

    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccountSecurityBatch(id=31, tenant_id=1, action_types='["self_heal_session"]'))
        session.commit()
        user = SimpleNamespace(
            has_permission=lambda permission: permission == "accounts.security.batch",
        )

        with pytest.raises(Exception) as exc_info:
            account_security_router._require_retry_batch_permissions(session, 1, 31, user)

        assert getattr(exc_info.value, "status_code", None) == 403
        assert getattr(exc_info.value, "detail", "") == "accounts.security.session_manage required"


def test_standby_session_batch_retry_rejects_malformed_action_types() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccountSecurityBatch(id=32, tenant_id=1, action_types="not-json"))
        session.commit()
        user = SimpleNamespace(has_permission=lambda _permission: True)

        with pytest.raises(Exception) as exc_info:
            account_security_router._require_retry_batch_permissions(session, 1, 32, user)

        assert getattr(exc_info.value, "status_code", None) == 400
        assert getattr(exc_info.value, "detail", "") == "invalid batch action_types"


def test_backend_account_search_matches_authorization_gap_and_rescue_states() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        rescue = TgAccount(id=20, tenant_id=1, display_name="可救援账号", phone_masked="20", status=AccountStatus.ACTIVE.value)
        gap = TgAccount(id=21, tenant_id=1, display_name="缺备用账号", phone_masked="21", status=AccountStatus.ACTIVE.value)
        session.add_all([rescue, gap])
        session.flush()
        session.add_all(
            [
                TgAccountAuthorization(tenant_id=1, account_id=20, role="primary", status="expired", health_status="expired", session_ciphertext="p"),
                TgAccountAuthorization(tenant_id=1, account_id=20, role="standby_1", status="standby", health_status="healthy", session_ciphertext="s1"),
                TgAccountAuthorization(tenant_id=1, account_id=21, role="primary", status="active", health_status="healthy", session_ciphertext="p"),
            ]
        )
        session.commit()
        accounts = list(session.scalars(select(TgAccount).where(TgAccount.id.in_([20, 21])).order_by(TgAccount.id)))

        rescue_matches = filter_accounts_by_search(session, accounts, "可从备用 session 激活恢复")
        gap_matches = filter_accounts_by_search(session, accounts, "standby_1 session 缺失")

        assert [account.id for account in rescue_matches] == [20]
        assert [account.id for account in gap_matches] == [21]


def test_backend_account_search_matches_capacity_and_device_summary_terms() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        capacity = TgAccount(id=27, tenant_id=1, display_name="容量账号", phone_masked="27", status=AccountStatus.ACTIVE.value)
        device = TgAccount(id=28, tenant_id=1, display_name="设备账号", phone_masked="28", status=AccountStatus.ACTIVE.value)
        session.add_all([capacity, device])
        session.flush()
        session.add_all(
            [
                AccountRuntimeSummary(
                    tenant_id=1,
                    account_id=27,
                    remaining_capacity=99,
                    unavailable_reason="账号冷却已结束",
                    failure_trend={"capacity_explanation": "小时剩余 99 / 100，账号冷却已结束"},
                ),
                AccountRuntimeSummary(
                    tenant_id=1,
                    account_id=28,
                    remaining_capacity=80,
                    unavailable_reason="安全状态待刷新",
                    failure_trend={"external_authorization_count": 2},
                ),
            ]
        )
        session.commit()
        accounts = list(session.scalars(select(TgAccount).where(TgAccount.id.in_([27, 28])).order_by(TgAccount.id)))

        capacity_matches = filter_accounts_by_search(session, accounts, "容量 99/100")
        cooldown_matches = filter_accounts_by_search(session, accounts, "账号冷却")
        device_matches = filter_accounts_by_search(session, accounts, "非平台设备")
        refresh_matches = filter_accounts_by_search(session, accounts, "安全待刷新")

        assert [account.id for account in capacity_matches] == [27]
        assert [account.id for account in cooldown_matches] == [27]
        assert [account.id for account in device_matches] == [28]
        assert [account.id for account in refresh_matches] == [28]


def test_frontend_account_list_exposes_capacity_and_state_search_terms() -> None:
    view = (PROJECT_ROOT.parent / "frontend/src/app/views/AccountsView.tsx").read_text()

    assert "capacity_explanation" in view
    assert "account_cooldown" in view
    assert "容量 ${availability.remaining_capacity}" in view
    assert "安全待刷新" in view
    assert "非平台设备" in view


def test_poll_verification_codes_does_not_create_mock_code(monkeypatch) -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        account = TgAccount(
            id=22,
            tenant_id=1,
            display_name="接码验证账号",
            phone_masked="22",
            status=AccountStatus.ACTIVE.value,
            session_ciphertext="session",
        )
        session.add(account)
        session.commit()
        monkeypatch.setattr(accounts_service, "credentials_for_account", lambda *_args, **_kwargs: SimpleNamespace())
        monkeypatch.setattr(accounts_service.gateway, "poll_verification_codes", lambda *_args, **_kwargs: [])
        monkeypatch.setattr(accounts_service, "get_settings", lambda: SimpleNamespace(tg_gateway_mode="mock"))

        codes = accounts_service.poll_account_verification_codes(session, 22, "tester", "检查官方验证码")

        assert codes == []
        assert session.scalar(select(func.count(TgVerificationCode.id))) == 0


def test_mock_gateway_does_not_emit_fake_official_verification_codes() -> None:
    assert TelegramGateway().poll_verification_codes(1, "session", SimpleNamespace()) == []


def test_code_receiver_is_blocked_in_task_precheck_and_dispatcher() -> None:
    precheck_source = (PROJECT_ROOT / "app/services/task_center/precheck.py").read_text()
    dispatcher_source = (PROJECT_ROOT / "app/services/task_center/dispatcher.py").read_text()

    assert 'TgAccount.account_identity != "code_receiver"' in precheck_source
    assert 'account.account_identity == "code_receiver"' in dispatcher_source
    assert "接码专用账号不参与任务执行" in dispatcher_source


def test_code_receiver_is_excluded_from_direct_task_candidate_queries() -> None:
    required_files = [
        PROJECT_ROOT / "app/services/task_center/listener_runtime.py",
        PROJECT_ROOT / "app/services/task_center/membership_admission.py",
        PROJECT_ROOT / "app/services/task_center/channel_membership.py",
        PROJECT_ROOT / "app/services/account_online_projection.py",
        PROJECT_ROOT / "app/services/account_online_state.py",
    ]

    for path in required_files:
        assert 'TgAccount.account_identity != "code_receiver"' in path.read_text(), path


def test_execution_records_aggregate_legacy_messages_and_task_actions() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        account = TgAccount(id=7, tenant_id=1, display_name="记录账号", phone_masked="7", status=AccountStatus.ACTIVE.value)
        session.add(account)
        session.flush()
        session.add(
            MessageTask(
                id=1,
                tenant_id=1,
                account_id=7,
                content="旧消息",
                idempotency_key="legacy-1",
                status="sent",
            )
        )
        session.add(Task(id="task-1", tenant_id=1, name="AI 群聊", type="group_ai_chat", status="running"))
        session.add(
            Action(
                id="action-1",
                tenant_id=1,
                task_id="task-1",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=7,
                status="success",
            )
        )
        session.add(
            ExecutionAttempt(
                id="attempt-1",
                tenant_id=1,
                action_id="action-1",
                account_id=7,
                attempt_no=1,
                status="after_call",
                remote_message_id="1001",
            )
        )
        session.add(
            ExecutionAttempt(
                id="attempt-2",
                tenant_id=1,
                action_id="action-1",
                account_id=7,
                attempt_no=2,
                status="after_call",
                remote_message_id="1002",
            )
        )
        session.add(
            Action(
                id="action-2",
                tenant_id=1,
                task_id="task-1",
                task_type="group_ai_chat",
                action_type="like_message",
                account_id=7,
                status="success",
            )
        )
        session.commit()

        records = account_execution_records(session, 7)

        assert [record["id"] for record in records].count("action:action-1:attempt-2") == 1
        assert "action:action-1:attempt-1" not in [record["id"] for record in records]
        assert [record["source"] for record in records] == ["task_action", "task_action", "message_task"]
        by_id = {record["id"]: record for record in records}
        assert by_id["action:action-1:attempt-2"]["remote_message_id"] == "1002"
        assert by_id["action:action-1:attempt-2"]["action_label"] == "发消息"
        assert by_id["action:action-2:latest"]["action_label"] == "点赞消息"
        assert records[2]["action_label"] == "私发消息"


def test_pending_execution_recheck_requeues_existing_failed_actions_without_duplicates() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        account = TgAccount(id=8, tenant_id=1, display_name="待处理账号", phone_masked="8", status=AccountStatus.ACTIVE.value)
        task = Task(id="task-2", tenant_id=1, name="群聊任务", type="group_ai_chat", status="running")
        failed = Action(
            id="action-failed",
            tenant_id=1,
            task_id="task-2",
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=8,
            status="failed",
            result={"error_code": "account_not_ready"},
        )
        pending = Action(
            id="action-pending",
            tenant_id=1,
            task_id="task-2",
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=8,
            status="pending",
        )
        session.add_all([account, task, failed, pending])
        session.commit()

        result = recheck_account_pending_execution(session, 8, "tester")

        assert result["requeued_count"] == 1
        assert result["existing_pending_count"] == 1
        assert session.get(Action, "action-failed").status == "pending"
        assert session.scalar(select(func.count(Action.id)).where(Action.account_id == 8)) == 2


def test_pending_execution_recheck_uses_group_send_permission_as_blocker() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        account = TgAccount(id=24, tenant_id=1, display_name="权限账号", phone_masked="24", status=AccountStatus.ACTIVE.value)
        group = TgGroup(id=24, tenant_id=1, tg_peer_id="g24", title="目标群", auth_status=GroupAuthStatus.AUTHORIZED.value, can_send=True)
        task = Task(id="task-24", tenant_id=1, name="群聊任务", type="group_ai_chat", status="running")
        blocked = Action(
            id="action-blocked",
            tenant_id=1,
            task_id="task-24",
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=24,
            status="failed",
            payload={"group_id": 24},
        )
        session.add_all([account, group, task, blocked])
        session.commit()

        blocked_result = recheck_account_pending_execution(session, 24, "tester")

        assert blocked_result["requeued_count"] == 0
        assert blocked_result["blocker_count"] == 1
        assert "账号尚未具备目标群发送权限" in blocked_result["blockers"][0]["reason"]
        assert session.get(Action, "action-blocked").status == "failed"

        session.add(TgGroupAccount(tenant_id=1, group_id=24, account_id=24, can_send=True))
        session.commit()

        recovered_result = recheck_account_pending_execution(session, 24, "tester")

        assert recovered_result["requeued_count"] == 1
        assert recovered_result["blocker_count"] == 0
        assert session.get(Action, "action-blocked").status == "pending"
