from dataclasses import replace

import pytest
from sqlalchemy.orm import Session

from tests.owner_postgres_support import owner_engine

pytestmark = pytest.mark.isolated_postgres
from app.security import encrypt_session
from app.services.developer_apps import credentials_for_authorization
from app.telegram_owner.fencing import validate_credentials
from app.telegram_owner.session_identity import authorization_identity
from test_account_direct_egress_postgres import _seed
from test_telegram_owner_client_cache import raw_session


def _prepare(engine):
    with Session(engine) as session:
        account, authorization = _seed(session)
        authorization.session_ciphertext = encrypt_session(raw_session())
        account.session_ciphertext = authorization.session_ciphertext
        session.commit()
        return credentials_for_authorization(session, authorization)


def test_owner_reads_current_generation_in_independent_transaction(owner_engine):
    engine = owner_engine
    credentials = _prepare(engine)
    validate_credentials(lambda: Session(engine), credentials, authorization_identity(raw_session()))
    from app.models import TgAccount

    with Session(engine) as other:
        account = other.get(TgAccount, credentials.account_id)
        account.connection_generation += 1
        other.commit()
    with pytest.raises(ValueError, match='account_generation_changed'):
        validate_credentials(lambda: Session(engine), credentials, authorization_identity(raw_session()))


@pytest.mark.parametrize('change', ['fact', 'app', 'session', 'tenant'])
def test_owner_fences_changed_authorization_facts(owner_engine, change):
    engine = owner_engine
    credentials = _prepare(engine)
    identities = authorization_identity(raw_session())
    if change == 'fact':
        credentials = replace(credentials, authorization_fact_version=credentials.authorization_fact_version + 1)
    if change == 'app':
        credentials = replace(credentials, app_id=credentials.app_id + 1)
    if change == 'tenant':
        credentials = replace(credentials, tenant_id=credentials.tenant_id + 1)
    if change == 'session':
        identities = authorization_identity(raw_session('other-account'))
    with pytest.raises(ValueError, match='telegram_owner_'):
        validate_credentials(lambda: Session(engine), credentials, identities)


def test_owner_rejects_newly_invalid_auth_even_without_fact_version(owner_engine):
    from app.models import TgAccountAuthorization
    from app.telegram_owner.errors import TelegramOwnerRequestRejected

    credentials = replace(_prepare(owner_engine), authorization_fact_version=None)
    with Session(owner_engine) as other:
        authorization = other.get(TgAccountAuthorization, credentials.authorization_id)
        authorization.health_status = 'invalid'
        other.commit()
    with pytest.raises(TelegramOwnerRequestRejected) as rejected:
        validate_credentials(lambda: Session(owner_engine), credentials, authorization_identity(raw_session()))
    assert rejected.value.remote_mutation_started is False
    assert rejected.value.transport_termination_acknowledged is True


def test_login_app_credentials_are_fenced_before_new_authorization(owner_engine):
    from app.models import TelegramDeveloperApp
    from app.telegram_owner.errors import TelegramOwnerRequestRejected

    credentials = replace(_prepare(owner_engine), account_id=None, authorization_id=None)
    with Session(owner_engine) as other:
        app = other.get(TelegramDeveloperApp, credentials.app_id)
        app.credentials_version += 1
        other.commit()
    with pytest.raises(TelegramOwnerRequestRejected, match='developer_app_changed'):
        validate_credentials(lambda: Session(owner_engine), credentials, None)
