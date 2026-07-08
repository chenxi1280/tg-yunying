from sqlalchemy import create_engine
from sqlalchemy.orm import Session
import pytest

from app.api.routers import accounts as accounts_router
from app.auth import CurrentUser
from app.api.response_permissions import account_out_for_user, accounts_out_for_user
from app.database import Base
from app.integrations.telegram.gateway import TelethonTelegramGateway
from app.integrations.telegram.mock import TelegramGateway
from app.integrations.telegram.contracts import AccountHealth, LoginChallenge
from app.models import Action, AccountProxy, AccountStatus, FailureType, Task, TelegramDeveloperApp, Tenant, TgAccount, TgAccountAuthorization, TgLoginFlow
from app.security import decrypt_session, encrypt_secret
from app.services import account_authorizations as authorization_service
from app.services import accounts as accounts_service
from app.services.task_center import dispatcher
from app.services.account_authorizations import (
    activate_authorization,
    attempt_standby_authorization_recovery,
    attempt_primary_proxy_recovery,
    authorization_summary_for_account,
    list_account_authorizations,
    refresh_authorization_slot,
    self_heal_authorizations,
    start_standby_authorization_login,
    switch_primary_authorization,
    verify_standby_authorization_login,
)
from app.services.accounts import health_check_account
from app.services.developer_apps import credentials_for_account, credentials_for_task_account


def _sqlite_session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _user() -> CurrentUser:
    return CurrentUser(
        id=1,
        tenant_id=1,
        name="admin",
        role="系统管理员",
        role_template="admin",
        email="admin@example.com",
        phone=None,
        tenant_name="默认运营空间",
        subscription_status="active",
        subscription_started_at=None,
        subscription_expires_at=None,
        subscription_days_remaining=0,
        can_use_core_features=True,
        token_balance=0,
        token_quota_total=0,
        menu_permissions=["accounts.view"],
        permissions=["accounts.view", "accounts.security.read", "accounts.sensitive.read"],
        permission_version=1,
        is_active=True,
    )


def test_legacy_account_projects_primary_authorization_without_blocking_standby_gap() -> None:
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            TelegramDeveloperApp(
                id=31,
                app_name="主应用",
                api_id=12345,
                api_hash_ciphertext="encrypted",
                is_active=True,
                health_status="健康",
            )
        )
        account = TgAccount(
            id=11,
            tenant_id=1,
            display_name="主授权账号",
            phone_masked="11",
            status=AccountStatus.ACTIVE.value,
            developer_app_id=31,
            developer_app_version=1,
            session_ciphertext="legacy-session",
            health_score=96,
        )
        session.add(account)
        session.commit()

        summary = authorization_summary_for_account(session, account)

        assert summary["primary_status"] == "active"
        assert summary["primary_source"] == "legacy_account"
        assert summary["standby_count"] == 0
        assert summary["has_standby"] is False
        assert summary["is_blocking"] is False
        assert summary["risk_hint"] == "未配置备用授权，主 session 失效时需要扫码或验证码恢复"


def test_account_response_includes_authorization_summary_for_legacy_account() -> None:
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        account = TgAccount(
            id=12,
            tenant_id=1,
            display_name="兼容账号",
            phone_masked="12",
            status=AccountStatus.ACTIVE.value,
            session_ciphertext="legacy-session",
            health_score=92,
        )
        session.add(account)
        session.commit()

        data = account_out_for_user(account, _user())

        assert data["authorization_summary"]["primary_status"] == "active"
        assert data["authorization_summary"]["standby_count"] == 0
        assert data["authorization_summary"]["is_blocking"] is False


def test_account_list_response_uses_authorization_summaries() -> None:
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        accounts = [
            TgAccount(
                id=121,
                tenant_id=1,
                display_name="兼容账号A",
                phone_masked="121",
                status=AccountStatus.ACTIVE.value,
                session_ciphertext="legacy-session",
                health_score=92,
            ),
            TgAccount(
                id=122,
                tenant_id=1,
                display_name="缺失账号B",
                phone_masked="122",
                status=AccountStatus.PENDING_LOGIN.value,
                health_score=0,
            ),
        ]
        session.add_all(accounts)
        session.commit()

        data = accounts_out_for_user(accounts, _user())

        assert data[0]["authorization_summary"]["primary_status"] == "active"
        assert data[0]["authorization_summary"]["is_blocking"] is False
        assert data[1]["authorization_summary"]["primary_status"] == "missing"
        assert data[1]["authorization_summary"]["is_blocking"] is True


def test_explicit_authorizations_count_healthy_standby_sessions() -> None:
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        account = TgAccount(
            id=13,
            tenant_id=1,
            display_name="主备账号",
            phone_masked="13",
            status=AccountStatus.ACTIVE.value,
            health_score=90,
        )
        session.add(account)
        session.flush()
        session.add_all(
            [
                TgAccountAuthorization(
                    tenant_id=1,
                    account_id=account.id,
                    role="primary",
                    session_ciphertext="primary-session",
                    status="active",
                    is_current=True,
                ),
                TgAccountAuthorization(
                    tenant_id=1,
                    account_id=account.id,
                    role="standby_1",
                    session_ciphertext="standby-session",
                    status="standby",
                    is_current=False,
                ),
            ]
        )
        session.commit()

        summary = authorization_summary_for_account(session, account)

        assert summary["primary_status"] == "active"
        assert summary["primary_source"] == "authorization_asset"
        assert summary["standby_count"] == 1
        assert summary["has_standby"] is True
        assert summary["risk_hint"] == ""


def test_legacy_primary_stays_primary_when_first_standby_is_added() -> None:
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        account = TgAccount(
            id=131,
            tenant_id=1,
            display_name="存量主授权加备用",
            phone_masked="131",
            status=AccountStatus.ACTIVE.value,
            session_ciphertext="legacy-session",
            health_score=90,
        )
        session.add(account)
        session.flush()
        session.add(
            TgAccountAuthorization(
                tenant_id=1,
                account_id=account.id,
                role="standby_1",
                session_ciphertext="standby-session",
                status="standby",
                is_current=False,
            )
        )
        session.commit()

        summary = authorization_summary_for_account(session, account)

        assert summary["primary_status"] == "active"
        assert summary["primary_source"] == "legacy_account"
        assert summary["standby_count"] == 1
        assert summary["has_standby"] is True
        assert summary["risk_hint"] == ""


def test_list_account_authorizations_projects_legacy_primary() -> None:
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        account = TgAccount(
            id=14,
            tenant_id=1,
            display_name="存量主授权账号",
            phone_masked="14",
            status=AccountStatus.ACTIVE.value,
            developer_app_id=31,
            proxy_id=41,
            session_ciphertext="legacy-session",
            health_score=91,
        )
        session.add(account)
        session.commit()

        rows = list_account_authorizations(session, account.id)

        assert rows == [
            {
                "id": None,
                "account_id": 14,
                "role": "primary",
                "developer_app_id": 31,
                "developer_app_api_id": None,
                "proxy_id": 41,
                "status": "active",
                "health_status": "legacy",
                "derived_status": "healthy",
                "is_current": True,
                "session_available": True,
                "primary_source": "legacy_account",
                "failure_reason": "",
                "last_health_check_at": None,
                "last_success_at": None,
                "last_switched_at": None,
                "disabled_at": None,
            }
        ]


def test_switch_primary_authorization_promotes_standby_and_preserves_legacy_primary() -> None:
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TelegramDeveloperApp(id=31, app_name="主应用", api_id=31001, api_hash_ciphertext="encrypted"),
                TelegramDeveloperApp(id=32, app_name="备用应用", api_id=32001, api_hash_ciphertext="encrypted"),
                AccountProxy(id=41, tenant_id=1, name="主代理", port=10041),
                AccountProxy(id=42, tenant_id=1, name="备用代理", port=10042),
            ]
        )
        account = TgAccount(
            id=15,
            tenant_id=1,
            display_name="可切换账号",
            phone_masked="15",
            status=AccountStatus.SESSION_EXPIRED.value,
            developer_app_id=31,
            developer_app_version=1,
            proxy_id=41,
            session_ciphertext="legacy-session",
            health_score=20,
        )
        session.add(account)
        session.flush()
        standby = TgAccountAuthorization(
            id=1501,
            tenant_id=1,
            account_id=account.id,
            role="standby_1",
            developer_app_id=32,
            proxy_id=42,
            session_ciphertext="standby-session",
            status="standby",
            is_current=False,
        )
        session.add(standby)
        session.commit()

        switched = switch_primary_authorization(session, account.id, standby.id, actor="admin", reason="主 session 失效")
        rows = list(session.query(TgAccountAuthorization).filter_by(account_id=account.id).order_by(TgAccountAuthorization.id))
        old_row = next(row for row in rows if row.session_ciphertext == "legacy-session")
        new_row = next(row for row in rows if row.session_ciphertext == "standby-session")

        assert switched.status == AccountStatus.ACTIVE.value
        assert switched.session_ciphertext == "standby-session"
        assert switched.developer_app_id == 32
        assert switched.proxy_id == 42
        assert old_row.role == "standby_repair"
        assert old_row.status == "needs_repair"
        assert new_row.role == "primary"
        assert new_row.status == "active"
        assert new_row.is_current is True


def test_switch_primary_authorization_rejects_standby_without_session() -> None:
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        account = TgAccount(
            id=16,
            tenant_id=1,
            display_name="无效备用账号",
            phone_masked="16",
            status=AccountStatus.SESSION_EXPIRED.value,
            health_score=20,
        )
        session.add(account)
        session.flush()
        standby = TgAccountAuthorization(
            tenant_id=1,
            account_id=account.id,
            role="standby_1",
            status="standby",
            is_current=False,
        )
        session.add(standby)
        session.commit()

        try:
            switch_primary_authorization(session, account.id, standby.id, actor="admin", reason="主 session 失效")
        except ValueError as exc:
            assert str(exc) == "备用授权没有可用 session"
        else:
            raise AssertionError("switch should reject standby without session")


@pytest.mark.no_postgres
def test_authorization_refresh_marks_target_waiting_for_code_with_healthy_source() -> None:
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        account = TgAccount(id=17, tenant_id=1, display_name="互救账号", phone_masked="17", status=AccountStatus.ACTIVE.value)
        session.add(account)
        session.flush()
        target = TgAccountAuthorization(
            id=1701,
            tenant_id=1,
            account_id=17,
            role="primary",
            status="active",
            health_status="expired",
            session_ciphertext="old-primary",
        )
        source = TgAccountAuthorization(
            id=1702,
            tenant_id=1,
            account_id=17,
            role="standby_1",
            status="standby",
            health_status="healthy",
            session_ciphertext="healthy-standby",
        )
        session.add_all([target, source])
        session.commit()

        result = refresh_authorization_slot(session, 17, 1701, actor="admin", reason="主授权掉线")
        refreshed = session.get(TgAccountAuthorization, 1701)

        assert result["status"] == "waiting_code"
        assert result["source_authorization_id"] == 1702
        assert refreshed.derived_status == "waiting_code"
        assert refreshed.failure_reason == "等待健康槽位读取 Telegram 官方验证码：主授权掉线"


@pytest.mark.no_postgres
def test_standby_login_persists_developer_app_api_id_snapshot() -> None:
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TelegramDeveloperApp(id=33, app_name="备用应用", api_id=33001, api_hash_ciphertext=encrypt_secret("hash")))
        account = TgAccount(id=25, tenant_id=1, display_name="备用账号", phone_masked="25", status=AccountStatus.ACTIVE.value)
        flow = TgLoginFlow(
            tenant_id=1,
            account_id=25,
            method="code",
            status="等待验证码",
            authorization_role="standby_1",
            developer_app_id=33,
            proxy_id=0,
        )
        session.add_all([account, flow])
        session.commit()

        asset = authorization_service._finish_standby_login(session, account, flow, AccountStatus.ACTIVE.value, "raw-session", "tester")

        assert asset.developer_app_api_id_snapshot == 33001
        assert asset.telegram_authorization_hash_ciphertext


@pytest.mark.no_postgres
def test_authorization_refresh_rejects_all_down_auto_recovery() -> None:
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        account = TgAccount(id=18, tenant_id=1, display_name="全掉线账号", phone_masked="18", status=AccountStatus.SESSION_EXPIRED.value)
        session.add(account)
        session.add(TgAccountAuthorization(id=1801, tenant_id=1, account_id=18, role="primary", status="active", health_status="expired", session_ciphertext="old"))
        session.commit()

        try:
            refresh_authorization_slot(session, 18, 1801, actor="admin", reason="全部掉线")
        except ValueError as exc:
            assert str(exc) == "三槽位全部掉线，只能人工重新登录 / 扫码 / 手动验证码"
        else:
            raise AssertionError("refresh should reject all-down auto recovery")


@pytest.mark.no_postgres
def test_self_heal_activates_healthy_standby_before_refreshing_primary() -> None:
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TelegramDeveloperApp(id=33, app_name="主应用", api_id=33001, api_hash_ciphertext="encrypted"),
                TelegramDeveloperApp(id=34, app_name="备用应用", api_id=34001, api_hash_ciphertext="encrypted"),
                AccountProxy(id=43, tenant_id=1, name="主代理", port=10043),
                AccountProxy(id=44, tenant_id=1, name="备用代理", port=10044),
            ]
        )
        account = TgAccount(id=19, tenant_id=1, display_name="自愈账号", phone_masked="19", status=AccountStatus.SESSION_EXPIRED.value, developer_app_id=33, proxy_id=43, session_ciphertext="primary-old")
        session.add(account)
        standby = TgAccountAuthorization(id=1902, tenant_id=1, account_id=19, role="standby_1", developer_app_id=34, proxy_id=44, status="standby", health_status="healthy", session_ciphertext="standby-ok")
        session.add(standby)
        session.commit()

        result = self_heal_authorizations(session, 19, actor="admin", reason="巡检发现 primary 掉线")
        account = session.get(TgAccount, 19)

        assert result["status"] == "activated_standby"
        assert result["activated_authorization_id"] == 1902
        assert account.session_ciphertext == "standby-ok"
        assert account.status == AccountStatus.ACTIVE.value


@pytest.mark.no_postgres
def test_authorization_routes_expose_refresh_activate_and_self_heal_contracts() -> None:
    source = (accounts_router.__file__ and open(accounts_router.__file__, encoding="utf-8").read()) or ""

    assert '"/api/tg-accounts/{account_id}/authorizations/{authorization_id}/refresh"' in source
    assert '"/api/tg-accounts/{account_id}/authorizations/{authorization_id}/activate"' in source
    assert '"/api/tg-accounts/{account_id}/authorizations/self-heal"' in source


def test_attempt_standby_recovery_switches_first_healthy_standby() -> None:
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TelegramDeveloperApp(id=31, app_name="主应用", api_id=31001, api_hash_ciphertext="encrypted"),
                TelegramDeveloperApp(id=32, app_name="备用应用", api_id=32001, api_hash_ciphertext="encrypted"),
                AccountProxy(id=41, tenant_id=1, name="主代理", port=10041),
                AccountProxy(id=42, tenant_id=1, name="备用代理", port=10042),
            ]
        )
        account = TgAccount(
            id=161,
            tenant_id=1,
            display_name="自动恢复账号",
            phone_masked="161",
            status=AccountStatus.SESSION_EXPIRED.value,
            developer_app_id=31,
            proxy_id=41,
            session_ciphertext="primary-session",
            health_score=30,
        )
        session.add(account)
        session.flush()
        standby = TgAccountAuthorization(
            tenant_id=1,
            account_id=account.id,
            role="standby_1",
            developer_app_id=32,
            proxy_id=42,
            session_ciphertext="standby-session",
            status="standby",
        )
        session.add(standby)
        session.commit()

        recovered = attempt_standby_authorization_recovery(session, account, actor="system", reason="session 已失效")

        assert recovered is not None
        assert account.status == AccountStatus.ACTIVE.value
        assert account.session_ciphertext == "standby-session"
        assert account.developer_app_id == 32
        assert account.proxy_id == 42


def test_health_check_switches_standby_when_primary_session_expired(monkeypatch) -> None:
    class ExpiredGateway(TelegramGateway):
        def check_account_health(self, session_ciphertext, credentials=None):
            return AccountHealth(status=AccountStatus.NEED_RELOGIN.value, health_score=45, detail="session 已失效")

    monkeypatch.setattr(accounts_service, "gateway", ExpiredGateway())
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TelegramDeveloperApp(id=31, app_name="主应用", api_id=31001, api_hash_ciphertext="encrypted"),
                TelegramDeveloperApp(id=32, app_name="备用应用", api_id=32001, api_hash_ciphertext="encrypted"),
            ]
        )
        account = TgAccount(
            id=162,
            tenant_id=1,
            display_name="健康检查恢复账号",
            phone_masked="162",
            status=AccountStatus.ACTIVE.value,
            developer_app_id=31,
            developer_app_version=1,
            session_ciphertext="primary-session",
            health_score=90,
        )
        session.add(account)
        session.flush()
        session.add(
            TgAccountAuthorization(
                tenant_id=1,
                account_id=account.id,
                role="standby_1",
                developer_app_id=32,
                session_ciphertext="standby-session",
                status="standby",
            )
        )
        session.commit()

        checked = health_check_account(session, account.id)

        assert checked.status == AccountStatus.ACTIVE.value
        assert checked.session_ciphertext == "standby-session"
        assert checked.developer_app_id == 32


def test_health_check_proxy_failure_switches_proxy_before_standby(monkeypatch) -> None:
    class ProxyFailedGateway(TelegramGateway):
        def check_account_health(self, session_ciphertext, credentials=None):
            return AccountHealth(status=AccountStatus.NEED_RELOGIN.value, health_score=45, detail="proxy connect timeout")

    monkeypatch.setattr(accounts_service, "gateway", ProxyFailedGateway())
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                AccountProxy(id=41, tenant_id=1, name="坏代理", port=10041, status="unhealthy", alert_status="alerting"),
                AccountProxy(id=42, tenant_id=1, name="健康代理", port=10042, status="healthy", alert_status="normal"),
                TelegramDeveloperApp(id=31, app_name="主应用", api_id=31001, api_hash_ciphertext="encrypted"),
                TelegramDeveloperApp(id=32, app_name="备用应用", api_id=32001, api_hash_ciphertext="encrypted"),
            ]
        )
        account = TgAccount(
            id=166,
            tenant_id=1,
            display_name="健康检查换线账号",
            phone_masked="166",
            status=AccountStatus.ACTIVE.value,
            developer_app_id=31,
            proxy_id=41,
            session_ciphertext="primary-session",
            health_score=90,
        )
        session.add(account)
        session.flush()
        session.add(
            TgAccountAuthorization(
                tenant_id=1,
                account_id=account.id,
                role="standby_1",
                developer_app_id=32,
                proxy_id=42,
                session_ciphertext="standby-session",
                status="standby",
            )
        )
        session.commit()

        checked = health_check_account(session, account.id)

        assert checked.proxy_id == 42
        assert checked.session_ciphertext == "primary-session"
        assert checked.developer_app_id == 31
        assert checked.status == AccountStatus.ACTIVE.value


def test_proxy_recovery_reuses_primary_session_before_standby() -> None:
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                AccountProxy(id=41, tenant_id=1, name="坏代理", port=10041, status="unhealthy", alert_status="alerting"),
                AccountProxy(id=42, tenant_id=1, name="健康代理", port=10042, status="healthy", alert_status="normal"),
                TelegramDeveloperApp(id=31, app_name="主应用", api_id=31001, api_hash_ciphertext="encrypted"),
                TelegramDeveloperApp(id=32, app_name="备用应用", api_id=32001, api_hash_ciphertext="encrypted"),
            ]
        )
        account = TgAccount(
            id=164,
            tenant_id=1,
            display_name="换线优先账号",
            phone_masked="164",
            status=AccountStatus.ACTIVE.value,
            developer_app_id=31,
            proxy_id=41,
            session_ciphertext="primary-session",
            health_score=90,
        )
        session.add(account)
        session.flush()
        session.add(
            TgAccountAuthorization(
                tenant_id=1,
                account_id=account.id,
                role="standby_1",
                developer_app_id=32,
                proxy_id=42,
                session_ciphertext="standby-session",
                status="standby",
            )
        )
        session.commit()

        recovered = attempt_primary_proxy_recovery(session, account, actor="system", reason="proxy timeout")

        assert recovered is not None
        assert account.proxy_id == 42
        assert account.session_ciphertext == "primary-session"
        assert account.developer_app_id == 31
        assert account.status == AccountStatus.ACTIVE.value


def test_task_dispatch_proxy_failure_switches_proxy_before_standby() -> None:
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                AccountProxy(id=41, tenant_id=1, name="坏代理", port=10041, status="unhealthy", alert_status="alerting"),
                AccountProxy(id=42, tenant_id=1, name="健康代理", port=10042, status="healthy", alert_status="normal"),
                TelegramDeveloperApp(id=31, app_name="主应用", api_id=31001, api_hash_ciphertext="encrypted"),
                TelegramDeveloperApp(id=32, app_name="备用应用", api_id=32001, api_hash_ciphertext="encrypted"),
            ]
        )
        account = TgAccount(
            id=165,
            tenant_id=1,
            display_name="调度换线账号",
            phone_masked="165",
            status=AccountStatus.ACTIVE.value,
            developer_app_id=31,
            proxy_id=41,
            session_ciphertext="primary-session",
            health_score=90,
        )
        session.add(account)
        session.flush()
        session.add(
            TgAccountAuthorization(
                tenant_id=1,
                account_id=account.id,
                role="standby_1",
                developer_app_id=32,
                proxy_id=42,
                session_ciphertext="standby-session",
                status="standby",
            )
        )
        session.add(Task(id="task-proxy-recover", tenant_id=1, name="代理恢复任务", type="group_ai_chat"))
        action = Action(
            id="action-proxy-recover",
            tenant_id=1,
            task_id="task-proxy-recover",
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=account.id,
            status="executing",
        )
        session.add(action)
        session.commit()

        dispatcher._apply_send_result(action, account, False, failure_type=FailureType.ACCOUNT_UNAVAILABLE.value, detail="proxy connect timeout")

        assert account.proxy_id == 42
        assert account.session_ciphertext == "primary-session"
        assert account.developer_app_id == 31
        assert action.status == "pending"
        assert action.result["proxy_recovered"] is True
        assert "account_recovered" not in action.result


def test_task_dispatch_failure_switches_standby_for_auth_failure() -> None:
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TelegramDeveloperApp(id=31, app_name="主应用", api_id=31001, api_hash_ciphertext="encrypted"),
                TelegramDeveloperApp(id=32, app_name="备用应用", api_id=32001, api_hash_ciphertext="encrypted"),
            ]
        )
        account = TgAccount(
            id=163,
            tenant_id=1,
            display_name="调度恢复账号",
            phone_masked="163",
            status=AccountStatus.ACTIVE.value,
            developer_app_id=31,
            session_ciphertext="primary-session",
            health_score=90,
        )
        session.add(account)
        session.flush()
        session.add(
            TgAccountAuthorization(
                tenant_id=1,
                account_id=account.id,
                role="standby_1",
                developer_app_id=32,
                session_ciphertext="standby-session",
                status="standby",
            )
        )
        session.add(Task(id="task-auth-recover", tenant_id=1, name="认证恢复任务", type="group_ai_chat"))
        action = Action(
            id="action-auth-recover",
            tenant_id=1,
            task_id="task-auth-recover",
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=account.id,
            status="executing",
        )
        session.add(action)
        session.commit()

        dispatcher._apply_send_result(action, account, False, failure_type=FailureType.ACCOUNT_UNAVAILABLE.value, detail="session 已失效")

        assert account.status == AccountStatus.ACTIVE.value
        assert account.session_ciphertext == "standby-session"
        assert action.status == "pending"
        assert action.result["account_recovered"] is True
        assert action.result["recovered_authorization_id"]


def test_standby_authorization_login_creates_flow_without_overwriting_primary_status(monkeypatch) -> None:
    monkeypatch.setattr(authorization_service, "gateway", TelegramGateway())
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            TelegramDeveloperApp(id=32, app_name="备用应用", api_id=32001, api_hash_ciphertext="encrypted")
        )
        session.add(AccountProxy(id=42, tenant_id=1, name="备用代理", port=10042, status="healthy"))
        account = TgAccount(
            id=17,
            tenant_id=1,
            display_name="新增备用账号",
            phone_masked="17",
            status=AccountStatus.ACTIVE.value,
            session_ciphertext="primary-session",
            health_score=95,
        )
        session.add(account)
        session.commit()

        flow = start_standby_authorization_login(
            session,
            account.id,
            method="code",
            role="standby_1",
            developer_app_id=32,
            proxy_id=42,
            actor="admin",
        )
        session.refresh(account)

        assert flow.status == AccountStatus.WAITING_CODE.value
        assert flow.authorization_role == "standby_1"
        assert flow.developer_app_id == 32
        assert flow.proxy_id == 42
        assert account.status == AccountStatus.ACTIVE.value
        assert account.session_ciphertext == "primary-session"


def test_credentials_for_account_includes_current_proxy() -> None:
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            TelegramDeveloperApp(
                id=32,
                app_name="主应用",
                api_id=32001,
                api_hash_ciphertext="encrypted",
                is_active=True,
                health_status="健康",
            )
        )
        session.add(
            AccountProxy(
                id=42,
                tenant_id=1,
                name="当前代理",
                protocol="socks5",
                host="127.0.0.1",
                port=10042,
                username="proxy-user",
                password_ciphertext=encrypt_secret("proxy-pass"),
                status="healthy",
            )
        )
        account = TgAccount(
            id=171,
            tenant_id=1,
            display_name="带代理账号",
            phone_masked="171",
            status=AccountStatus.ACTIVE.value,
            developer_app_id=32,
            developer_app_version=1,
            proxy_id=42,
            session_ciphertext="primary-session",
            health_score=95,
        )
        session.add(account)
        session.commit()

        credentials = credentials_for_account(session, account)

        assert credentials.proxy_id == 42
        assert credentials.proxy_protocol == "socks5"
        assert credentials.proxy_host == "127.0.0.1"
        assert credentials.proxy_port == 10042
        assert credentials.proxy_username == "proxy-user"
        assert credentials.proxy_password == "proxy-pass"


@pytest.mark.no_postgres
def test_credentials_for_account_can_explicitly_bypass_proxy() -> None:
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            TelegramDeveloperApp(
                id=32,
                app_name="主应用",
                api_id=32001,
                api_hash_ciphertext=encrypt_secret("hash"),
                is_active=True,
                health_status="健康",
            )
        )
        session.add(AccountProxy(id=42, tenant_id=1, name="备用代理", protocol="socks5", host="10.0.0.42", port=10042, status="healthy"))
        account = TgAccount(
            id=171,
            tenant_id=1,
            display_name="带代理账号",
            phone_masked="171",
            status=AccountStatus.ACTIVE.value,
            developer_app_id=32,
            developer_app_version=1,
            proxy_id=42,
            session_ciphertext="primary-session",
        )
        session.add(account)
        session.commit()

        credentials = credentials_for_account(session, account, use_proxy=False)

        assert credentials.api_id == 32001
        assert credentials.proxy_id is None
        assert credentials.proxy_host == ""


@pytest.mark.no_postgres
@pytest.mark.parametrize(
    ("task_type", "expected_proxy_id"),
    [
        ("group_ai_chat", None),
        ("channel_comment", None),
        ("channel_view", None),
        ("channel_like", None),
        ("search_join_group", 42),
    ],
)
def test_credentials_for_task_account_keeps_proxy_only_for_search_join(task_type: str, expected_proxy_id: int | None) -> None:
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            TelegramDeveloperApp(
                id=32,
                app_name="主应用",
                api_id=32001,
                api_hash_ciphertext=encrypt_secret("hash"),
                is_active=True,
                health_status="健康",
            )
        )
        session.add(AccountProxy(id=42, tenant_id=1, name="备用代理", protocol="socks5", host="10.0.0.42", port=10042, status="healthy"))
        account = TgAccount(
            id=171,
            tenant_id=1,
            display_name="带代理账号",
            phone_masked="171",
            status=AccountStatus.ACTIVE.value,
            developer_app_id=32,
            developer_app_version=1,
            proxy_id=42,
        )
        session.add(account)
        session.commit()

        credentials = credentials_for_task_account(session, account, task_type)

        assert credentials.proxy_id == expected_proxy_id


def test_standby_authorization_login_uses_selected_proxy_credentials(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class RecordingGateway(TelegramGateway):
        def start_login(self, method, account_id=None, phone=None, credentials=None):
            captured["proxy_id"] = credentials.proxy_id if credentials else None
            captured["proxy_host"] = credentials.proxy_host if credentials else ""
            return LoginChallenge(status=AccountStatus.WAITING_CODE.value, code_preview="12345")

    monkeypatch.setattr(authorization_service, "gateway", RecordingGateway())
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            TelegramDeveloperApp(
                id=32,
                app_name="备用应用",
                api_id=32001,
                api_hash_ciphertext="encrypted",
                is_active=True,
                health_status="健康",
            )
        )
        session.add(AccountProxy(id=42, tenant_id=1, name="备用代理", host="10.0.0.42", port=10042, status="healthy"))
        account = TgAccount(
            id=172,
            tenant_id=1,
            display_name="备用代理登录账号",
            phone_masked="172",
            status=AccountStatus.ACTIVE.value,
            session_ciphertext="primary-session",
            health_score=95,
        )
        session.add(account)
        session.commit()

        start_standby_authorization_login(
            session,
            account.id,
            method="code",
            role="standby_1",
            developer_app_id=32,
            proxy_id=42,
            actor="admin",
        )

        assert captured == {"proxy_id": 42, "proxy_host": "10.0.0.42"}


def test_verify_standby_authorization_login_saves_asset_without_overwriting_primary_session(monkeypatch) -> None:
    monkeypatch.setattr(authorization_service, "gateway", TelegramGateway())
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            TelegramDeveloperApp(id=32, app_name="备用应用", api_id=32001, api_hash_ciphertext="encrypted")
        )
        session.add(AccountProxy(id=42, tenant_id=1, name="备用代理", port=10042, status="healthy"))
        account = TgAccount(
            id=18,
            tenant_id=1,
            display_name="备用登录完成账号",
            phone_masked="18",
            status=AccountStatus.ACTIVE.value,
            session_ciphertext="primary-session",
            health_score=95,
        )
        session.add(account)
        session.commit()
        flow = start_standby_authorization_login(
            session,
            account.id,
            method="code",
            role="standby_2",
            developer_app_id=32,
            proxy_id=42,
            actor="admin",
        )

        asset = verify_standby_authorization_login(session, account.id, flow.id, code="12345", password_2fa=None, actor="admin")
        session.refresh(account)

        assert account.session_ciphertext == "primary-session"
        assert asset.role == "standby_2"
        assert asset.status == "standby"
        assert asset.developer_app_id == 32
        assert asset.proxy_id == 42
        assert asset.is_current is False
        assert decrypt_session(asset.session_ciphertext).startswith("encrypted-session:")


def test_accounts_router_exposes_authorization_asset_routes() -> None:
    route_keys = {(route.path, ",".join(sorted(route.methods))) for route in accounts_router.router.routes}

    assert ("/api/tg-accounts/{account_id}/authorizations", "GET") in route_keys
    assert ("/api/tg-accounts/{account_id}/authorizations/{authorization_id}/switch-primary", "POST") in route_keys
    assert ("/api/tg-accounts/{account_id}/authorizations/login/start", "POST") in route_keys
    assert ("/api/tg-accounts/{account_id}/authorizations/login/verify", "POST") in route_keys


def test_telegram_gateways_do_not_expose_reset_other_authorizations() -> None:
    assert not hasattr(TelegramGateway, "cleanup_other_authorizations")
    assert not hasattr(TelethonTelegramGateway, "cleanup_other_authorizations")
    assert not hasattr(TelethonTelegramGateway, "_cleanup_other_authorizations_async")
