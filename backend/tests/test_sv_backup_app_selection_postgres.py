import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DeveloperAppSlotAssignment, TelegramDeveloperApp, TgAccount, TgAccountAuthorization
from app.models import TgAuthorizationDrOperation
from app.services.authorization_dr import apply_abc_backup, preview_abc_backup
from app.services.authorization_dr.contracts import AuthorizationDrError
from tests.owner_postgres_support import owner_engine
from test_authorization_abc_backup import _seed

pytestmark = pytest.mark.isolated_postgres


@pytest.fixture
def session(owner_engine):
    with Session(owner_engine) as db:
        _seed(db)
        db.add_all([
            DeveloperAppSlotAssignment(slot_purpose='primary_sv', developer_app_id=1,
                                       assignment_version=1, credentials_version=1, assigned_by='test'),
            DeveloperAppSlotAssignment(slot_purpose='standby_2_my', developer_app_id=3,
                                       assignment_version=1, credentials_version=1, assigned_by='test'),
        ])
        db.commit()
        yield db


def current_app(session, app_id):
    account = session.get(TgAccount, 101)
    primary = session.get(TgAccountAuthorization, account.current_authorization_id)
    account.developer_app_id = app_id
    primary.developer_app_id = app_id
    session.commit()
    return primary


def c_slot(session, app_id, *, region='my'):
    row = TgAccountAuthorization(tenant_id=1, account_id=101, role='standby_2',
                                logical_slot='standby_2', provision_region_code=region,
                                developer_app_id=app_id, status='standby', health_status='healthy',
                                is_slot_current=True, is_current=False, slot_generation=4, fact_version=7)
    session.add(row)
    session.commit()
    return row


def preview(session):
    return preview_abc_backup(session, 1, 101, idempotency_key='existing-c-selection')


@pytest.mark.parametrize('primary_app,c_app,expected', [(2, 1, 3), (1, 3, 2), (3, 2, 1)])
def test_existing_my_app_is_preserved_and_b_uses_the_third_app(session, primary_app, c_app, expected):
    primary = current_app(session, primary_app)
    c = c_slot(session, c_app)
    before = (primary.id, primary.session_ciphertext, c.id, c.developer_app_id, c.fact_version)
    result = preview(session)
    assert result['app_b_id'] == expected
    assert result['preserved_c']['authorization_id'] == c.id
    assert result['preserved_c']['developer_app_id'] == c_app
    assert result['preserved_c']['fact_version'] == 7
    assert result['preserved_c']['slot_generation'] == 4
    assert result['preserved_c']['region'] == 'my'
    assert (primary.id, primary.session_ciphertext, c.id, c.developer_app_id, c.fact_version) == before
    assert session.scalar(select(TgAuthorizationDrOperation.id)) is None


def test_existing_sv_c_also_reserves_its_actual_app(session):
    current_app(session, 2)
    c = c_slot(session, 1, region='sv')
    result = preview(session)
    assert result['app_b_id'] == 3
    assert result['preserved_c']['authorization_id'] == c.id
    assert result['preserved_c']['region'] == 'sv'


@pytest.mark.parametrize('primary_app,expected', [(1, 2), (2, 1), (3, 2)])
def test_no_c_keeps_existing_assignment_preference(session, primary_app, expected):
    current_app(session, primary_app)
    result = preview(session)
    assert result['app_b_id'] == expected
    assert result['preserved_c'] is None


def test_stale_retained_c_is_not_current_c(session):
    current_app(session, 2)
    old = c_slot(session, 1)
    old.is_slot_current = False
    old.status = 'retained'
    session.commit()
    c_slot(session, 3)
    assert preview(session)['app_b_id'] == 1


def test_c_version_change_is_rejected_before_creating_operation(session, monkeypatch):
    current_app(session, 2)
    c = c_slot(session, 1)
    frozen = preview(session)
    with Session(session.bind) as other:
        changed = other.get(TgAccountAuthorization, c.id)
        changed.fact_version += 1
        other.commit()
    monkeypatch.setattr('app.services.authorization_dr.abc_backup._execute_b_login',
                        lambda *_: pytest.fail('login must not run after C drift'))
    with pytest.raises(AuthorizationDrError) as exc:
        apply_abc_backup(session, 1, 101, idempotency_key=frozen['idempotency_key'],
                         expected_fingerprint=frozen['fingerprint'], requested_by='requester',
                         approved_by='approver', approval_ref='test-c-drift')
    assert exc.value.code == 'migration_fingerprint_conflict'
    assert session.scalar(select(TgAuthorizationDrOperation.id)) is None


def test_unavailable_remaining_app_cannot_reuse_the_my_app(session):
    current_app(session, 2)
    c_slot(session, 1)
    session.get(TelegramDeveloperApp, 3).is_active = False
    session.commit()
    with pytest.raises(AuthorizationDrError) as exc:
        preview(session)
    assert exc.value.code == 'developer_app_slot_assignment_conflict'


def test_duplicate_c_metadata_is_explicitly_rejected(session):
    current_app(session, 2)
    c_slot(session, 1)
    c_slot(session, 3)
    with pytest.raises(AuthorizationDrError) as exc:
        preview(session)
    assert exc.value.code == 'migration_source_standby_not_unique'
