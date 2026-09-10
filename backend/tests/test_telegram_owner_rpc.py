import multiprocessing
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.config import Settings
from app.integrations.telegram import AuthorizationIdentity
from app.telegram_owner.errors import TelegramOwnerOutcomeUnknown, TelegramOwnerUnavailable
from app.telegram_owner.request_context import bind_identity, reset_identity, expected_instance
from app.telegram_owner.rpc import OwnerGateway
from app.telegram_owner.server import OwnerServer, _claim_instance
from app.telegram_owner.session_identity import authorization_identity
from test_telegram_owner_client_cache import raw_session

pytestmark = pytest.mark.no_postgres


class InstrumentedGateway:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.maximum = 0
        self.by_key = {}

    def authorization_identity(self, raw_session, credentials=None):
        import time

        key = authorization_identity(raw_session)
        with self.lock:
            self.active += 1
            self.maximum = max(self.active, self.maximum)
            self.by_key[key] = self.by_key.get(key, 0) + 1
            assert self.by_key[key] == 1
        time.sleep(0.05)
        with self.lock:
            self.active -= 1
            self.by_key[key] -= 1
            maximum = self.maximum
        return AuthorizationIdentity(str(maximum), key, expected_instance(), 'authorization')


def _serve(path, ready):
    settings = Settings(telegram_owner_socket=path, session_secret_key='unit-test-ipc', telegram_owner_mode='server')
    server = OwnerServer(settings, InstrumentedGateway(), None)
    ready.set()
    server.serve_forever()


@pytest.fixture
def owner():
    with tempfile.TemporaryDirectory(prefix='tg-owner-', dir='/tmp') as directory:
        context = multiprocessing.get_context('spawn')
        ready = context.Event()
        path = str(Path(directory) / 'owner.sock')
        process = context.Process(target=_serve, args=(path, ready))
        process.start()
        assert ready.wait(10)
        gateway = OwnerGateway(Settings(telegram_owner_socket=path, session_secret_key='unit-test-ipc'))
        try:
            yield gateway, process, directory
        finally:
            process.terminate()
            process.join(5)
            assert not process.is_alive()


def test_cross_process_calls_share_authkey_owner_and_other_accounts_run_concurrently(owner):
    gateway, _, _ = owner
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: gateway.authorization_identity(raw_session()), range(8)))
    assert all(result.authorization_hash == '1' for result in results)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda value: gateway.authorization_identity(raw_session(str(value))), range(4)))
    assert max(int(result.authorization_hash) for result in results) > 1


def test_second_owner_cannot_remove_or_replace_live_socket(owner):
    gateway, _, directory = owner
    before = gateway.call('__status__')
    with pytest.raises(RuntimeError, match='already_running'):
        _claim_instance(Path(directory))
    assert gateway.call('__status__')['instance_id'] == before['instance_id']


def test_owner_executor_receives_actual_server_instance_without_caller_hint(owner):
    gateway, _, _ = owner
    actual = gateway.call('__status__')['instance_id']
    assert gateway.authorization_identity(raw_session()).telegram_user_id_digest == actual


def test_stale_owner_instance_is_rejected_before_execution(owner):
    gateway, _, _ = owner
    token = bind_identity('telegram-gateway:test', 'previous-instance')
    try:
        with pytest.raises(ValueError, match='instance_changed_before_issue'):
            gateway.authorization_identity(raw_session())
    finally:
        reset_identity(token)
    assert gateway.authorization_identity(raw_session()).authorization_hash == '1'


def test_owner_exit_releases_host_lock(owner):
    _, process, directory = owner
    process.terminate()
    process.join(5)
    with _claim_instance(Path(directory)):
        assert not process.is_alive()


def test_connection_failure_is_confirmed_before_submit(monkeypatch):
    def unavailable(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr('app.telegram_owner.rpc.Client', unavailable)
    with pytest.raises(TelegramOwnerUnavailable) as failure:
        OwnerGateway(Settings()).call('authorization_identity', raw_session())
    assert failure.value.remote_mutation_started is False


def test_lost_response_is_unknown_and_not_retried(monkeypatch):
    calls = []

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def send_bytes(self, request): calls.append(request)
        def recv_bytes(self): raise EOFError

    monkeypatch.setattr('app.telegram_owner.rpc.Client', lambda *args, **kwargs: Connection())
    with pytest.raises(TelegramOwnerOutcomeUnknown) as failure:
        OwnerGateway(Settings()).call('authorization_identity', raw_session())
    assert failure.value.transport_termination_acknowledged is False
    assert len(calls) == 1


def test_planner_cannot_bypass_remote_io_prohibition_through_owner(monkeypatch):
    from app.telethon_lifecycle import TelethonClientLifecycle

    monkeypatch.setattr('app.telegram_owner.rpc.Client', lambda *args, **kwargs: pytest.fail('planner reached IPC'))
    TelethonClientLifecycle.set_runtime_role('planner')
    try:
        with pytest.raises(RuntimeError, match='planner_remote_io_forbidden'):
            OwnerGateway(Settings()).authorization_identity(raw_session())
    finally:
        TelethonClientLifecycle.set_runtime_role('all')
