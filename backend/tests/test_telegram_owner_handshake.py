"""Regression for a peer disconnecting during Listener authentication."""
import logging
import socket
from types import SimpleNamespace

import pytest

from app.telegram_owner.server import OwnerServer
from test_telegram_owner_rpc import owner

pytestmark = pytest.mark.no_postgres


class AcceptComplete(Exception):
    pass


@pytest.mark.parametrize('failure', [EOFError, BrokenPipeError, ConnectionResetError])
def test_handshake_disconnect_is_logged_and_accept_continues(failure, caplog):
    calls = []

    def accept():
        calls.append(True)
        if len(calls) == 1:
            raise failure('test peer disconnected')
        raise AcceptComplete

    server = SimpleNamespace(_listener=SimpleNamespace(accept=accept))
    with caplog.at_level(logging.WARNING), pytest.raises(AcceptComplete):
        OwnerServer.serve_forever(server)
    assert len(calls) == 2
    assert 'telegram_owner_ipc_handshake_disconnected' in caplog.text
    assert failure.__name__ in caplog.text


def test_non_peer_oserror_is_not_hidden():
    def accept():
        raise OSError(24, 'Too many open files')

    server = SimpleNamespace(_listener=SimpleNamespace(accept=accept))
    with pytest.raises(OSError) as result:
        OwnerServer.serve_forever(server)
    assert result.value.errno == 24


@pytest.mark.parametrize('read_challenge', [False, True])
def test_real_peer_disconnect_preserves_owner_instance(owner, read_challenge):
    gateway, process, directory = owner
    before = gateway.call('__status__')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(3)
        peer.connect(f'{directory}/owner.sock')
        if read_challenge:
            assert peer.recv(1024)
    after = gateway.call('__status__')
    assert process.is_alive()
    assert after['instance_id'] == before['instance_id']
    assert after['pid'] == before['pid']
