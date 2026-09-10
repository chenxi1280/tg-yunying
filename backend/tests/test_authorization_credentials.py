import pytest
from app.models import AccountProxy, AccountStatus, TelegramDeveloperApp, Tenant, TgAccount, TgAccountAuthorization
from app.security import encrypt_secret
from app.services.developer_apps import credentials_for_account, credentials_for_authorization
from test_account_authorizations import _sqlite_session

pytestmark = pytest.mark.no_postgres

@pytest.mark.no_postgres
def test_credentials_for_account_uses_direct_credentials_by_default() -> None:
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

        assert credentials.api_id == 32001
        assert credentials.proxy_id is None
        assert credentials.proxy_host == ""



@pytest.mark.no_postgres
def test_credentials_for_account_rejects_explicit_proxy() -> None:
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

        with pytest.raises(ValueError, match="telegram_account_proxy_forbidden"):
            credentials_for_account(session, account, use_proxy=True)



@pytest.mark.no_postgres
def test_credentials_for_authorization_uses_direct_primary_regular_by_default() -> None:
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TelegramDeveloperApp(
            id=32, app_name="主应用", api_id=32001,
            api_hash_ciphertext=encrypt_secret("hash"),
            is_active=True, health_status="健康",
        ))
        session.add(AccountProxy(
            id=42, tenant_id=1, name="历史代理", protocol="socks5",
            host="127.0.0.1", port=10042, status="healthy",
        ))
        session.add(TgAccount(id=171, tenant_id=1, display_name="account", phone_masked="171",
                              developer_app_id=32, developer_app_version=1))
        authorization = TgAccountAuthorization(
            tenant_id=1, account_id=171, role="primary", logical_slot="primary",
            developer_app_id=32, developer_app_api_id_snapshot=32001,
            proxy_id=42, session_ciphertext="session", status="active",
        )
        session.add(authorization)
        session.commit()

        direct = credentials_for_authorization(session, authorization)
        with pytest.raises(ValueError, match="telegram_account_proxy_forbidden"):
            credentials_for_authorization(session, authorization, use_proxy=True)

        assert direct.proxy_id is None
        assert direct.account_id == 171
        assert direct.authorization_id == authorization.id
        assert direct.authorization_fact_version == authorization.fact_version



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
