from types import SimpleNamespace

import pytest

from app.services.authorization_dr.contracts import AuthorizationDrError
from app.services.authorization_dr.local_activate import _require_switchable_target

pytestmark = pytest.mark.no_postgres


def _target(**changes):
    fields = dict(logical_slot='standby_1', role='standby_1', is_current=False,
                  disabled_at=None, is_slot_current=True, provision_region_code='sv',
                  status='standby', health_status='healthy', session_ciphertext='test',
                  developer_app_id=2)
    return SimpleNamespace(**(fields | changes))


@pytest.mark.parametrize('slot', ['primary', 'standby_1'])
def test_healthy_sv_business_standby_accepts_complementary_physical_slot(slot):
    _require_switchable_target(_target(logical_slot=slot))


@pytest.mark.parametrize('changes', [
    {'logical_slot': 'standby_2'}, {'role': 'standby_2'}, {'role': 'standby_repair'},
    {'role': 'primary'}, {'is_current': True}, {'disabled_at': 'disabled'},
    {'is_slot_current': False}, {'provision_region_code': 'my'},
    {'status': 'retained'}, {'health_status': 'invalid'},
    {'session_ciphertext': ''}, {'developer_app_id': None},
])
def test_complementary_slot_does_not_admit_invalid_or_nonbusiness_standby(changes):
    with pytest.raises(AuthorizationDrError):
        _require_switchable_target(_target(**({'logical_slot': 'primary'} | changes)))
