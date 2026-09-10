"""Carry the existing task request identity across private owner IPC."""
from contextvars import ContextVar

_REQUEST_IDENTITY = ContextVar('telegram_owner_request_identity', default=('', ''))


def bind_identity(identity: str, instance_id: str = ""):
    return _REQUEST_IDENTITY.set((identity, instance_id))


def reset_identity(token):
    _REQUEST_IDENTITY.reset(token)


def current_identity() -> str:
    return _REQUEST_IDENTITY.get()[0]


def expected_instance() -> str:
    return _REQUEST_IDENTITY.get()[1]
