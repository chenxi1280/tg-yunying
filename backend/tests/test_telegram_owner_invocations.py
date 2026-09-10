import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.integrations.telegram import DeveloperAppCredentials, SendResult
from app.integrations.telegram.update_contracts import TelegramDifferenceBatch, TelegramNormalizedUpdate
from app.telegram_owner import codec
from app.telegram_owner.executor import InvocationExecutor
from app.telegram_owner.errors import TelegramOwnerRequestRejected
from app.telethon_lifecycle import TelethonClientLifecycle, TelethonOperationTimeout
from test_telegram_owner_client_cache import raw_session
from test_telethon_lifecycle import reset_lifecycle_state

pytestmark = pytest.mark.no_postgres


class Journal:
    def __init__(self): self.rows = []
    def write(self, request, state, **facts): self.rows.append((state, facts))


def _request():
    return {'request_id': str(uuid4()), 'method': 'authorization_identity',
            'args': (raw_session(),), 'kwargs': {}}


def test_json_roundtrip_preserves_contract_types_and_binary_fields():
    update = TelegramDifferenceBatch('account', 'complete', {'pts': 3},
                                     (TelegramNormalizedUpdate('id', 'type'),))
    original = {'credentials': DeveloperAppCredentials(1, 2, 'synthetic-test-secret', 3),
                'result': SendResult(True, '12', remote_fact={'at': datetime.now(timezone.utc)}),
                'data': b'\x00\xff', 'set': frozenset({'a'}), 'update': update}
    assert codec.loads(codec.dumps(original)) == original


@pytest.mark.parametrize('body', [
    b'{"type":"pickle","value":"ignored"}',
    b'{"type":"contract","name":"RunCode","fields":{}}',
])
def test_json_does_not_load_arbitrary_types(body):
    with pytest.raises((KeyError, ValueError)):
        codec.loads(body)


def test_cancelled_caller_never_issues_gateway_call():
    class Gateway:
        def authorization_identity(self, raw_session): pytest.fail('caller already exited')

    journal = Journal()
    executor = InvocationExecutor(Gateway(), None, journal, methods={'authorization_identity'})
    with pytest.raises(TelegramOwnerRequestRejected, match='caller_closed_before_issue'):
        executor.execute(_request(), caller_connected=lambda: False)
    assert all(state != 'issued' for state, _ in journal.rows)


def test_unacknowledged_timeout_keeps_owner_until_runner_really_exits():
    reset_lifecycle_state()
    terminated = threading.Event()
    invoked_again = threading.Event()

    class Gateway:
        def __init__(self): self.calls = 0
        def authorization_identity(self, raw_session):
            self.calls += 1
            if self.calls == 1:
                raise TelethonOperationTimeout(transport_termination_acknowledged=False,
                                               termination_event=terminated)
            invoked_again.set()
            return 'second'

    journal = Journal()
    executor = InvocationExecutor(Gateway(), None, journal, methods={'authorization_identity'})
    with pytest.raises(TelethonOperationTimeout):
        executor.execute(_request(), caller_connected=lambda: True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(executor.execute, _request(), caller_connected=lambda: True)
        assert not invoked_again.wait(0.05)
        assert TelethonClientLifecycle._active_keys
        terminated.set()
        assert future.result(timeout=2) == 'second'
    assert not TelethonClientLifecycle._active_keys
    assert any(state == 'transport_terminated_business_unknown' for state, _ in journal.rows)
    reset_lifecycle_state()


@pytest.mark.parametrize('failure_stage', ['issued', 'completed'])
def test_journal_failure_preserves_submission_boundary(failure_stage):
    class BrokenJournal:
        def write(self, request, state, **facts):
            if state == failure_stage:
                raise OSError('test disk unavailable')

    class Gateway:
        calls = 0

        def authorization_identity(self, raw_session):
            self.calls += 1
            return 'remote-result'

    gateway = Gateway()
    executor = InvocationExecutor(gateway, None, BrokenJournal(), methods={'authorization_identity'})
    expected = TelegramOwnerRequestRejected if failure_stage == 'issued' else TelethonOperationTimeout
    with pytest.raises(expected) as error:
        executor.execute(_request(), caller_connected=lambda: True)
    assert gateway.calls == (0 if failure_stage == 'issued' else 1)
    assert error.value.transport_termination_acknowledged is True
    assert not TelethonClientLifecycle._active_keys


def test_stale_credentials_can_close_their_old_cached_connection():
    from app.security import encrypt_session

    class Gateway:
        def invalidate_session_cache(self, session_ciphertext, credentials):
            return 1

    def forbidden_validation():
        pytest.fail('closing an old connection must not require current credentials')

    request = {'request_id': str(uuid4()), 'method': 'invalidate_session_cache',
               'args': (encrypt_session(raw_session()), DeveloperAppCredentials(1, 2, 'old', 1)), 'kwargs': {}}
    executor = InvocationExecutor(Gateway(), forbidden_validation, Journal(), methods={'invalidate_session_cache'})
    assert executor.execute(request, caller_connected=lambda: True) == 1
    assert not TelethonClientLifecycle._active_keys
