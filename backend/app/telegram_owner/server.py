"""A host-fenced single owner; a second instance cannot bind while the first lives."""
from __future__ import annotations

import fcntl
import os
import logging
from multiprocessing import AuthenticationError
import signal
import socket
import threading
from multiprocessing.connection import Listener
from pathlib import Path
from uuid import uuid4

from . import codec
from .callbacks import restore_callbacks
from .errors import exception_payload, TelegramOwnerRequestRejected
from .executor import InvocationExecutor
from .journal import InvocationJournal
from .rpc import gateway_methods, ipc_key


class OwnerServer:
    def __init__(self, settings, gateway, session_factory):
        self.settings = settings
        self.instance_id = str(uuid4())
        directory = Path(settings.telegram_owner_socket).parent
        self._lock_file = _claim_instance(directory)
        socket_path = Path(settings.telegram_owner_socket)
        socket_path.unlink(missing_ok=True)
        self._listener = Listener(str(socket_path), family='AF_UNIX', authkey=ipc_key(settings), backlog=socket.SOMAXCONN)
        socket_path.chmod(0o600)
        journal = InvocationJournal(directory / 'invocations', self.instance_id)
        self.executor = InvocationExecutor(gateway, session_factory, journal, methods=gateway_methods())
        self._threads = set()
        self._threads_lock = threading.Lock()

    def serve_forever(self):
        while True:
            try:
                connection = self._listener.accept()
            except AuthenticationError:
                logging.getLogger(__name__).warning("telegram_owner_ipc_authentication_rejected")
                continue
            except (EOFError, BrokenPipeError, ConnectionResetError) as exc:
                logging.getLogger(__name__).warning(
                    "telegram_owner_ipc_handshake_disconnected error_type=%s", type(exc).__name__
                )
                continue
            thread = threading.Thread(target=self._handle, args=(connection,), daemon=False)
            with self._threads_lock:
                self._threads.add(thread)
            thread.start()

    def _handle(self, connection):
        try:
            with connection:
                request = codec.loads(connection.recv_bytes())
                response = self._response(request, connection)
                connection.send_bytes(codec.dumps(response))
        except (EOFError, BrokenPipeError, ConnectionResetError):
            # The caller lost its response. Issued mutation receipts remain in the journal.
            return
        finally:
            with self._threads_lock:
                self._threads.discard(threading.current_thread())

    def _response(self, request, connection):
        try:
            if request.get('method') == '__status__':
                return {'ok': True, 'result': self.status()}
            if request.get('expected_instance') not in {None, '', self.instance_id}:
                raise TelegramOwnerRequestRejected('telegram_owner_instance_changed_before_issue')
            result = self._execute_owned(restore_callbacks(request, connection), connection)
            return {'ok': True, 'result': result, 'instance_id': self.instance_id}
        except Exception as exc:
            return {'ok': False, 'error': exception_payload(exc), 'instance_id': self.instance_id}

    def _execute_owned(self, request, connection):
        from .request_context import bind_identity, reset_identity

        token = bind_identity(str(request.get('root_identity') or ''), self.instance_id)
        try:
            return self.executor.execute(request, caller_connected=lambda: not connection.poll())
        finally:
            reset_identity(token)

    def status(self):
        from app.telethon_lifecycle import TelethonClientLifecycle

        return {'instance_id': self.instance_id, 'pid': os.getpid(),
                'release_sha': os.getenv('RELEASE_SHA', ''),
                'connected_clients': TelethonClientLifecycle.connected_client_count(),
                'pending_login_clients': len(getattr(self.executor.gateway, '_pending_clients', {}))}

    def close(self):
        self._listener.close()
        with self._threads_lock:
            threads = tuple(self._threads)
        for thread in threads:
            thread.join()
        from app.telethon_lifecycle import shutdown_telethon_lifecycle_strict

        for flow_id in tuple(getattr(self.executor.gateway, '_pending_clients', {})):
            self.executor.gateway.cancel_login(flow_id)
        shutdown_telethon_lifecycle_strict()
        self._lock_file.close()


def _claim_instance(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    descriptor = os.open(directory / 'owner.lock', os.O_RDWR | os.O_CREAT, 0o600)
    file = os.fdopen(descriptor, 'r+')
    try:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        file.close()
        raise RuntimeError('telegram_owner_instance_already_running') from None
    return file


def main():
    from app.config import get_settings
    from app.database import SessionLocal
    from app.integrations.telegram.gateway import TelethonTelegramGateway

    settings = get_settings()
    if settings.telegram_owner_mode != 'server':
        raise ValueError('telegram_owner_server_mode_required')
    server = OwnerServer(settings, TelethonTelegramGateway(settings), SessionLocal)
    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)
    try:
        server.serve_forever()
    finally:
        server.close()


def _terminate(_signal, _frame):
    raise SystemExit(0)


if __name__ == '__main__':
    main()
