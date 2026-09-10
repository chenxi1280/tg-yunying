"""Serialize by AuthKey while keeping unrelated accounts independently runnable."""
from __future__ import annotations

import inspect
import threading
from contextlib import ExitStack
from uuid import UUID

from app.security import decrypt_session
from app.telethon_lifecycle import TelethonClientLifecycle, TelethonOperationTimeout

from .fencing import validate_credentials
from .errors import TelegramOwnerRequestRejected
from .session_identity import authorization_identity


class InvocationExecutor:
    def __init__(self, gateway, session_factory, journal, *, methods):
        self.gateway = gateway
        self.session_factory = session_factory
        self.journal = journal
        self.methods = methods
        self._locks = {}
        self._registry_lock = threading.Lock()

    def execute(self, request, *, caller_connected):
        method, arguments = self._validate(request)
        keys, identity = _ownership_keys(arguments)
        scope = self._acquire(keys)
        retain = False
        try:
            if not caller_connected():
                raise TelegramOwnerRequestRejected('telegram_owner_caller_closed_before_issue')
            if request['method'] != 'invalidate_session_cache':
                validate_credentials(self.session_factory, arguments.get('credentials'), identity)
            self._record_before_issue(request, identity, arguments=arguments)
            try:
                result = method(*request['args'], **request['kwargs'])
            except TelethonOperationTimeout as exc:
                retain = not exc.transport_termination_acknowledged
                self._observe_timeout(request, scope, exc, retain=retain)
                raise
            except Exception as exc:
                self._record_after_issue(request, 'failed', error_type=type(exc).__name__)
                raise
            self._record_after_issue(request, 'completed', remote_message_id=getattr(result, 'remote_message_id', None))
            return result
        finally:
            if not retain:
                scope.close()

    def _record_before_issue(self, request, identity, *, arguments):
        try:
            credentials = arguments.get('credentials')
            account_id = arguments.get('account_id') or getattr(credentials, 'account_id', None)
            self.journal.write(request, 'issued', authorization_identity=identity,
                               account_id=account_id, flow_id=arguments.get('flow_id'))
        except OSError as exc:
            raise TelegramOwnerRequestRejected('telegram_owner_journal_failed_before_issue') from exc

    def _record_after_issue(self, request, state, **facts):
        try:
            self.journal.write(request, state, **facts)
        except OSError as exc:
            raise TelethonOperationTimeout(transport_termination_acknowledged=True) from exc

    def _validate(self, request):
        UUID(request['request_id'])
        if request['method'] not in self.methods:
            raise ValueError('telegram_owner_method_forbidden')
        method = getattr(self.gateway, request['method'])
        arguments = inspect.signature(method).bind(*request['args'], **request['kwargs']).arguments
        return method, arguments

    def _acquire(self, keys):
        scope = ExitStack()
        with self._registry_lock:
            locks = tuple(self._locks.setdefault(key, threading.Lock()) for key in sorted(keys))
        for lock in locks:
            scope.enter_context(lock)
        identities = tuple(key for key in keys if not key.startswith('login-flow:'))
        TelethonClientLifecycle.pin_authorizations(identities)
        scope.callback(TelethonClientLifecycle.unpin_authorizations, identities)
        return scope

    def _observe_timeout(self, request, scope, exc, *, retain):
        event = exc.termination_event
        if retain and event is not None:
            threading.Thread(target=self._release_after_termination,
                             args=(request, scope, event), daemon=True).start()
        try:
            self.journal.write(request, 'result_unknown', termination_acknowledged=not retain)
        except OSError as journal_error:
            raise exc from journal_error

    def _release_after_termination(self, request, scope, event):
        event.wait()
        try:
            self.journal.write(request, 'transport_terminated_business_unknown')
        finally:
            scope.close()


def _ownership_keys(arguments):
    raw = arguments.get('raw_session') or arguments.get('temporary_session')
    if not raw and arguments.get('session_ciphertext'):
        raw = decrypt_session(arguments['session_ciphertext'])
    identity = authorization_identity(raw) if raw else None
    keys = {identity} if identity else set()
    if arguments.get('flow_id') is not None:
        keys.add('login-flow:' + str(arguments['flow_id']))
    return keys, identity
