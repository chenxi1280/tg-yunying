import asyncio
import multiprocessing
import os
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from app.config import Settings
from app.integrations.telegram.search_join import (
    ImageVerificationDecision, ImageVerificationRequest, ImageVerificationVote,
    ImageVerificationRuntimeContractError, ImageVerificationConsensusUnavailableError,
)
from app.telegram_owner import codec
from app.telegram_owner.callbacks import CallbackChannel, CallbackClient, restore_callbacks
from app.telegram_owner.errors import TelegramOwnerOutcomeUnknown, TelegramOwnerRequestRejected, TelegramOwnerCallbackError
from app.telegram_owner.rpc import OwnerGateway
from app.telegram_owner.server import OwnerServer

pytestmark = pytest.mark.no_postgres


class SearchGateway:
    def execute_search_join(self, account_id, payload, session_ciphertext=None,
                            credentials=None, keyword_text='', image_verification_solver=None):
        request = ImageVerificationRequest(b'image-bytes', 'image/png', ('A', 'B'), 'choose')
        async def recognize():
            try:
                first = await asyncio.to_thread(image_verification_solver, request)
            except ImageVerificationRuntimeContractError as exc:
                return {'code': exc.code, 'votes': exc.votes,
                        'deadline': exc.callback_submit_deadline_monotonic}
            except ImageVerificationConsensusUnavailableError as exc:
                return {'consensus_unavailable': str(exc), 'votes': exc.votes}
            second = await asyncio.to_thread(image_verification_solver, request)
            return {'first': first, 'second': second, 'owner_pid': os.getpid()}

        return asyncio.run(recognize())


def _serve(path, ready):
    settings = Settings(telegram_owner_socket=path, session_secret_key='unit-test-callback')
    server = OwnerServer(settings, SearchGateway(), None)
    ready.set()
    server.serve_forever()


@pytest.fixture
def owner():
    with tempfile.TemporaryDirectory(prefix='tg-callback-', dir='/tmp') as directory:
        context = multiprocessing.get_context('spawn')
        ready = context.Event()
        path = str(Path(directory) / 'owner.sock')
        process = context.Process(target=_serve, args=(path, ready))
        process.start()
        assert ready.wait(10)
        gateway = OwnerGateway(Settings(telegram_owner_socket=path, session_secret_key='unit-test-callback'))
        try:
            yield gateway, process
        finally:
            process.terminate()
            process.join(5)
            assert not process.is_alive()


def test_real_process_callback_preserves_typed_request_decision_and_caller_execution(owner):
    gateway, process = owner
    calls = []
    decision = ImageVerificationDecision('A', 0.99, (ImageVerificationVote('ocr', 'accepted', 'A'),))

    def solver(request):
        calls.append((os.getpid(), request))
        return decision

    result = gateway.execute_search_join(1, {}, image_verification_solver=solver)
    assert result == {'first': decision, 'second': decision, 'owner_pid': process.pid}
    assert len(calls) == 2
    assert all(pid == os.getpid() and request.image_bytes == b'image-bytes' for pid, request in calls)
    assert all(request.candidate_answers == ('A', 'B') for _, request in calls)


def test_positional_solver_and_none_decision_are_transported(owner):
    gateway, _ = owner
    result = gateway.execute_search_join(1, {}, None, None, '', lambda request: None)
    assert result['first'] is None and result['second'] is None


def test_callback_error_is_returned_without_fake_success(owner):
    gateway, _ = owner

    def solver(request):
        raise ValueError('recognition_failed')

    with pytest.raises(TelegramOwnerCallbackError, match='recognition_failed'):
        gateway.execute_search_join(1, {}, image_verification_solver=solver)


def test_invalid_decision_is_explicit_error(owner):
    gateway, _ = owner
    with pytest.raises(TelegramOwnerCallbackError, match='decision_required'):
        gateway.execute_search_join(1, {}, image_verification_solver=lambda request: {'answer': 'A'})


def test_encoding_failure_is_known_before_submission(monkeypatch):
    monkeypatch.setattr('app.telegram_owner.rpc.Client', lambda *a, **kw: pytest.fail('submitted'))
    with pytest.raises(TelegramOwnerRequestRejected) as failure:
        OwnerGateway(Settings()).authorization_identity(lambda: None)
    assert failure.value.remote_mutation_started is False


def test_unknown_callback_reference_cannot_call_local_code():
    client = CallbackClient(str(uuid4()))
    frame = {'type': 'callback', 'request_id': client.request_id,
             'callback_id': str(uuid4()), 'call_id': str(uuid4())}
    with pytest.raises(TelegramOwnerCallbackError, match='identity_mismatch'):
        client._respond(None, frame)


def test_foreign_invocation_callback_is_rejected():
    client = CallbackClient(str(uuid4()))
    _, kwargs = client.prepare('execute_search_join', (), {'image_verification_solver': lambda value: None})
    frame = {'type': 'callback', 'request_id': str(uuid4()),
             'callback_id': kwargs['image_verification_solver']['owner_callback_id'], 'call_id': str(uuid4())}
    with pytest.raises(TelegramOwnerCallbackError, match='identity_mismatch'):
        client._respond(None, frame)


def test_owner_rejects_non_reference_solver_before_issue():
    request = {'method': 'execute_search_join', 'request_id': str(uuid4()),
               'args': (1, {}), 'kwargs': {'image_verification_solver': 'not-code'}}
    with pytest.raises(TelegramOwnerRequestRejected, match='reference_invalid'):
        restore_callbacks(request, None)


def test_callback_disconnect_retains_submit_uncertainty(monkeypatch):
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def send_bytes(self, value): self.request = codec.loads(value)
        def recv_bytes(self): raise EOFError

    monkeypatch.setattr('app.telegram_owner.rpc.Client', lambda *a, **kw: Connection())
    with pytest.raises(TelegramOwnerOutcomeUnknown) as failure:
        OwnerGateway(Settings()).execute_search_join(1, {}, image_verification_solver=lambda value: None)
    assert failure.value.remote_mutation_started is None


def test_callback_result_identity_must_match():
    class Connection:
        def send_bytes(self, value): self.request = codec.loads(value)
        def recv_bytes(self):
            return codec.dumps({**self.request, 'type': 'callback_result', 'call_id': str(uuid4()),
                                'ok': True, 'result': None})

    channel = CallbackChannel(Connection(), str(uuid4()))
    with pytest.raises(TelegramOwnerCallbackError, match='response_identity_mismatch'):
        channel.call(str(uuid4()), ImageVerificationRequest(b'image', 'image/png', (), 'question'))


def test_callback_reply_disconnect_is_unknown_without_reexecuting_solver(monkeypatch):
    calls = []

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def send_bytes(self, value):
            frame = codec.loads(value)
            if frame.get('type') == 'callback_result':
                raise BrokenPipeError
            self.request = frame
        def recv_bytes(self):
            reference = self.request['kwargs']['image_verification_solver']
            return codec.dumps({'type': 'callback', 'request_id': self.request['request_id'],
                                'callback_id': reference['owner_callback_id'], 'call_id': str(uuid4()),
                                'argument': ImageVerificationRequest(b'image', 'image/png', (), 'question')})

    def solver(request):
        calls.append(request)
        return None

    monkeypatch.setattr('app.telegram_owner.rpc.Client', lambda *a, **kw: Connection())
    with pytest.raises(TelegramOwnerOutcomeUnknown):
        OwnerGateway(Settings()).execute_search_join(1, {}, image_verification_solver=solver)
    assert len(calls) == 1


def test_bad_callback_uuid_is_confirmed_before_issue():
    request = {'method': 'execute_search_join', 'request_id': str(uuid4()), 'args': (1, {}),
               'kwargs': {'image_verification_solver': {'owner_callback_id': 'invalid'}}}
    with pytest.raises(TelegramOwnerRequestRejected) as error:
        restore_callbacks(request, None)
    assert error.value.remote_mutation_started is False


def test_independent_callback_invocations_do_not_cross_wire(owner):
    from concurrent.futures import ThreadPoolExecutor

    gateway, _ = owner

    def invoke(answer):
        decision = ImageVerificationDecision(answer, 0.9, ())
        result = gateway.execute_search_join(1, {}, image_verification_solver=lambda request: decision)
        assert result['first'] == decision and result['second'] == decision
        return result['first'].answer

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(invoke, ('A', 'B', 'C', 'D'))) == ['A', 'B', 'C', 'D']


def test_runtime_contract_error_keeps_code_votes_and_original_deadline(owner):
    gateway, _ = owner
    votes = (ImageVerificationVote('local', 'unknown'),)

    def solver(request):
        raise ImageVerificationRuntimeContractError('verification_local_ocr_unknown', 'pending', votes, 123.5)

    result = gateway.execute_search_join(1, {}, image_verification_solver=solver)
    assert result == {'code': 'verification_local_ocr_unknown', 'votes': votes, 'deadline': 123.5}


def test_consensus_error_keeps_votes_for_gateway_handling(owner):
    gateway, _ = owner
    votes = (ImageVerificationVote('ocr', 'low_confidence'),)

    def solver(request):
        raise ImageVerificationConsensusUnavailableError('no consensus', votes)

    result = gateway.execute_search_join(1, {}, image_verification_solver=solver)
    assert result == {'consensus_unavailable': 'no consensus', 'votes': votes}
