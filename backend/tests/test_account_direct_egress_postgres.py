from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.database import engine
from app.models import AccountProxy, TelegramDeveloperApp, Tenant, TgAccount, TgAccountAuthorization
from app.security import encrypt_secret
from app.services.account_runtime_transport import task_account_runtime_transport
from app.services.developer_apps import credentials_for_account, credentials_for_authorization, credentials_for_task_account
from app.services.task_center.engagement_runtime_domains import proxy_domain_keys

TEST_ID = 987654


def _seed(session):
    session.add(Tenant(id=TEST_ID, name="direct transport test"))
    session.add(TelegramDeveloperApp(
        id=TEST_ID, app_name="direct test", api_id=TEST_ID,
        api_hash_ciphertext=encrypt_secret("test"), is_active=True, health_status="健康",
    ))
    session.flush()
    session.add(AccountProxy(id=TEST_ID, tenant_id=TEST_ID, name="retired", host="127.0.0.1", port=1080))
    session.flush()
    account = TgAccount(
        id=TEST_ID, tenant_id=TEST_ID, display_name="test", phone_masked="test",
        status="在线", developer_app_id=TEST_ID, developer_app_version=1,
        proxy_id=TEST_ID, session_ciphertext="stale-compatible-projection",
    )
    session.add(account)
    session.flush()
    authorization = TgAccountAuthorization(
        tenant_id=TEST_ID, account_id=TEST_ID, developer_app_id=TEST_ID,
        proxy_id=TEST_ID, is_current=True, status="active", health_status="healthy",
        session_ciphertext="canonical-session", provision_region_code="sv",
    )
    session.add(authorization)
    session.flush()
    account.current_authorization_id = authorization.id
    session.flush()
    return account, authorization


@pytest.mark.parametrize("task_type", ["group_ai_chat", "channel_view", "channel_like", "channel_comment"])
def test_current_authorization_and_direct_route_are_consistent(task_type):
    with Session(engine) as session:
        account, authorization = _seed(session)
        transport = task_account_runtime_transport(session, account, task_type)
        assert transport.session_ciphertext == authorization.session_ciphertext
        assert transport.authorization_id == authorization.id
        credentials = [transport.credentials, credentials_for_account(session, account),
                       credentials_for_authorization(session, authorization),
                       credentials_for_task_account(session, account, task_type)]
        assert all(c.proxy_id is None and not c.proxy_host for c in credentials)
        assert transport.dependency_snapshot["egress_id"] == "primary_regular:direct"
        assert transport.dependency_snapshot["proxy_id"] is None
        assert proxy_domain_keys(session, account) == ("", "")
        assert account.proxy_id == authorization.proxy_id == TEST_ID


@pytest.mark.parametrize("field,value", [
    ("health_status", "invalid"), ("provision_region_code", "my"),
    ("last_authoritative_error_code", "authorization_key_duplicated"),
])
def test_invalid_or_my_authorization_cannot_enter_sv_task_transport(field, value):
    with Session(engine) as session:
        account, authorization = _seed(session)
        setattr(authorization, field, value)
        with pytest.raises(ValueError, match="current_account_authorization_unavailable"):
            task_account_runtime_transport(session, account, "group_ai_chat")
