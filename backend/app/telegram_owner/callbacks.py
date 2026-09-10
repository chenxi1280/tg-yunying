"""Invocation-scoped OCR callbacks; executable values never cross the IPC boundary."""
from __future__ import annotations

import threading
from uuid import UUID, uuid4

from . import codec
from .errors import TelegramOwnerRequestRejected, TelegramOwnerCallbackError, exception_payload

SEARCH_METHOD = 'execute_search_join'
SOLVER_KEY = 'image_verification_solver'
SOLVER_POSITION = 5
REFERENCE_KEY = 'owner_callback_id'


class CallbackClient:
    def __init__(self, request_id):
        self.request_id = request_id
        self.callbacks = {}

    def prepare(self, method, args, kwargs):
        if method != SEARCH_METHOD:
            return args, kwargs
        values, options = list(args), dict(kwargs)
        if len(values) > SOLVER_POSITION:
            values[SOLVER_POSITION] = self._reference(values[SOLVER_POSITION])
        if SOLVER_KEY in options:
            options[SOLVER_KEY] = self._reference(options[SOLVER_KEY])
        return tuple(values), options

    def _reference(self, callback):
        if callback is None:
            return None
        if not callable(callback):
            raise TelegramOwnerRequestRejected('telegram_owner_solver_callable_required_before_submit')
        callback_id = str(uuid4())
        self.callbacks[callback_id] = callback
        return {REFERENCE_KEY: callback_id}

    def receive(self, connection):
        while True:
            response = codec.loads(connection.recv_bytes())
            if response.get('type') != 'callback':
                return response
            self._respond(connection, response)

    def _respond(self, connection, request):
        callback_id = request.get('callback_id')
        if request.get('request_id') != self.request_id or callback_id not in self.callbacks:
            raise TelegramOwnerCallbackError('telegram_owner_callback_identity_mismatch')
        try:
            UUID(request['call_id'])
        except (ValueError, TypeError, AttributeError) as exc:
            raise TelegramOwnerCallbackError('telegram_owner_callback_call_id_invalid') from exc
        response = {key: request[key] for key in ('request_id', 'callback_id', 'call_id')}
        response['type'] = 'callback_result'
        try:
            _require_request(request['argument'])
            result = self.callbacks[callback_id](request['argument'])
            _require_decision(result)
            response.update(ok=True, result=result)
        except Exception as exc:
            response.update(ok=False, error=_callback_error_payload(exc))
        connection.send_bytes(codec.dumps(response))


def restore_callbacks(request, connection):
    if request['method'] != SEARCH_METHOD:
        return request
    args, kwargs = list(request['args']), dict(request['kwargs'])
    channel = CallbackChannel(connection, request['request_id'])
    if len(args) > SOLVER_POSITION:
        args[SOLVER_POSITION] = channel.restore(args[SOLVER_POSITION])
    if SOLVER_KEY in kwargs:
        kwargs[SOLVER_KEY] = channel.restore(kwargs[SOLVER_KEY])
    return {**request, 'args': tuple(args), 'kwargs': kwargs}


class CallbackChannel:
    def __init__(self, connection, request_id):
        self.connection = connection
        self.request_id = request_id
        self.lock = threading.Lock()

    def restore(self, reference):
        if reference is None:
            return None
        if not isinstance(reference, dict) or set(reference) != {REFERENCE_KEY}:
            raise TelegramOwnerRequestRejected('telegram_owner_callback_reference_invalid_before_issue')
        callback_id = reference[REFERENCE_KEY]
        try:
            UUID(callback_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise TelegramOwnerRequestRejected('telegram_owner_callback_id_invalid_before_issue') from exc
        return lambda argument: self.call(callback_id, argument)

    def call(self, callback_id, argument):
        _require_request(argument)
        request = {'type': 'callback', 'request_id': self.request_id,
                   'callback_id': callback_id, 'call_id': str(uuid4()), 'argument': argument}
        with self.lock:
            self.connection.send_bytes(codec.dumps(request))
            response = codec.loads(self.connection.recv_bytes())
        if response.get('type') != 'callback_result' or any(
            response.get(key) != request[key] for key in ('request_id', 'callback_id', 'call_id')
        ):
            raise TelegramOwnerCallbackError('telegram_owner_callback_response_identity_mismatch')
        if not response['ok']:
            _raise_callback_error(response['error'])
        _require_decision(response['result'])
        return response['result']


def _require_request(value):
    from app.integrations.telegram.search_join import ImageVerificationRequest

    if not isinstance(value, ImageVerificationRequest):
        raise TypeError('telegram_owner_image_verification_request_required')


def _require_decision(value):
    from app.integrations.telegram.search_join import ImageVerificationDecision

    if value is not None and not isinstance(value, ImageVerificationDecision):
        raise TypeError('telegram_owner_image_verification_decision_required')


def _callback_error_payload(exc):
    from app.integrations.telegram.search_join import (
        ImageVerificationConsensusUnavailableError, ImageVerificationRuntimeContractError,
    )

    payload = exception_payload(exc)
    if isinstance(exc, (ImageVerificationRuntimeContractError, ImageVerificationConsensusUnavailableError)):
        payload['votes'] = exc.votes
    if isinstance(exc, ImageVerificationRuntimeContractError):
        payload.update(code=exc.code, callback_submit_deadline_monotonic=exc.callback_submit_deadline_monotonic)
    return payload


def _raise_callback_error(error):
    from app.integrations.telegram.search_join import (
        ImageVerificationConsensusUnavailableError, ImageVerificationRuntimeContractError,
    )

    if error['type'] == 'ImageVerificationRuntimeContractError':
        raise ImageVerificationRuntimeContractError(
            error['code'], error['message'], error['votes'], error['callback_submit_deadline_monotonic'],
        )
    if error['type'] == 'ImageVerificationConsensusUnavailableError':
        raise ImageVerificationConsensusUnavailableError(error['message'], error['votes'])
    raise TelegramOwnerCallbackError(
        'telegram_owner_callback_failed:' + error['type'] + ':' + error['message']
    )
