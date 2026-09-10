"""Authenticated host-local JSON RPC to the only Telegram connection owner."""
from __future__ import annotations

import functools
import hashlib
import hmac
import inspect
from multiprocessing.connection import Client
from uuid import uuid4

from . import codec
from .errors import TelegramOwnerOutcomeUnknown, TelegramOwnerUnavailable, raise_remote


@functools.lru_cache(maxsize=1)
def gateway_methods() -> frozenset[str]:
    from app.integrations.telegram.gateway import TelethonTelegramGateway

    return frozenset(name for name, method in vars(TelethonTelegramGateway).items()
                     if not name.startswith("_") and inspect.isfunction(method))


def ipc_key(settings) -> bytes:
    return hmac.new(settings.session_secret_key.encode(), b"tg-authorization-owner-ipc-v1", hashlib.sha256).digest()


class OwnerGateway:
    supports_group_clone_desired_state_probe = True
    supports_rank_deboost_observation = True

    def __init__(self, settings):
        self.settings = settings

    def __getattr__(self, name):
        if name not in gateway_methods():
            raise AttributeError(name)
        return functools.partial(self.call, name)

    def call(self, method: str, *args, **kwargs):
        from app.telethon_lifecycle import TelethonClientLifecycle
        from .request_context import current_identity, expected_instance

        if method != "__status__":
            TelethonClientLifecycle._assert_remote_io_allowed()

        request = codec.dumps({"method": method, "args": args, "kwargs": kwargs,
                               "request_id": str(uuid4()), "root_identity": current_identity(), "expected_instance": expected_instance()})
        try:
            connection = Client(self.settings.telegram_owner_socket, family="AF_UNIX", authkey=ipc_key(self.settings))
        except (OSError, EOFError) as exc:
            raise TelegramOwnerUnavailable("telegram_owner_unavailable_before_submit") from exc
        with connection:
            try:
                connection.send_bytes(request)
                response = codec.loads(connection.recv_bytes())
            except (OSError, EOFError) as exc:
                raise TelegramOwnerOutcomeUnknown() from exc
        if not response["ok"]:
            raise_remote(response["error"])
        return response["result"]
