from app.models import AccountStatus, Tenant, TelegramDeveloperApp, TgAccount, TgAccountAuthorization, TgAccountOnlineState
from app.security import encrypt_session


def seed_local_activation(session):
    session.add(Tenant(id=1, name='recovery'))
    session.flush()
    session.add_all([TelegramDeveloperApp(id=1, app_name='A', api_id=101, api_hash_ciphertext='test'),
                     TelegramDeveloperApp(id=2, app_name='B', api_id=102, api_hash_ciphertext='test')])
    session.flush()
    account = TgAccount(id=8, tenant_id=1, display_name='recovery', phone_masked='test',
                        developer_app_id=1, session_ciphertext=encrypt_session('old'),
                        authorization_generation=4, authorization_fact_generation=7,
                        connection_generation=9, status=AccountStatus.SESSION_EXPIRED.value)
    session.add(account)
    session.flush()
    primary = TgAccountAuthorization(id=1, tenant_id=1, account_id=8, role='primary',
                                     logical_slot='standby_1', is_current=True, developer_app_id=1,
                                     session_ciphertext=account.session_ciphertext, health_status='invalid')
    target = TgAccountAuthorization(id=2, tenant_id=1, account_id=8, role='standby_1',
                                    logical_slot='primary', is_current=False, developer_app_id=2,
                                    session_ciphertext=encrypt_session('standby'), status='standby',
                                    health_status='healthy', telegram_user_id_digest='b' * 64,
                                    auth_key_fingerprint_digest='a' * 64, fact_version=3)
    session.add_all([primary, target])
    session.flush()
    account.current_authorization_id = primary.id
    session.add(TgAccountOnlineState(tenant_id=1, account_id=8, online_status='login_required'))
    session.commit()
    return account, primary, target
