"""Explicit owner transport boundaries without assuming remote completion."""
from __future__ import annotations

from app.telethon_lifecycle import TelethonOperationTimeout


class TelegramOwnerUnavailable(ConnectionError):
    remote_mutation_started = False
    transport_termination_acknowledged = True


class TelegramOwnerRequestRejected(ValueError):
    remote_mutation_started = False
    transport_termination_acknowledged = True


class TelegramOwnerOutcomeUnknown(TelethonOperationTimeout):
    remote_mutation_started = None

    def __init__(self):
        super().__init__(transport_termination_acknowledged=False)
        self.args = ("telegram_owner_response_lost_after_submit",)


class TelegramOwnerCallbackError(RuntimeError):
    remote_mutation_started = None


class TelegramOwnerRemoteError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.remote_error_code = code


def exception_payload(exc: Exception) -> dict:
    return {"type": type(exc).__name__, "message": str(exc),
            "termination_acknowledged": getattr(exc, "transport_termination_acknowledged", None),
            "seconds": getattr(exc, "seconds", None)}


def raise_remote(payload: dict) -> None:
    name, message = payload["type"], payload["message"]
    if name == "TelegramOwnerCallbackError":
        raise TelegramOwnerCallbackError(message)
    if name == "TelegramOwnerRequestRejected":
        raise TelegramOwnerRequestRejected(message)
    if name == "TelethonOperationTimeout":
        raise TelethonOperationTimeout(
            transport_termination_acknowledged=payload["termination_acknowledged"] is True,
        )
    builtins = {"ValueError": ValueError, "TypeError": TypeError, "RuntimeError": RuntimeError,
                "TimeoutError": TimeoutError, "ConnectionError": ConnectionError}
    if name in builtins:
        raise builtins[name](message)
    _raise_telegram_error(name, payload)


def _raise_telegram_error(name: str, payload: dict) -> None:
    from telethon import errors

    cls = getattr(errors, name, None)
    if isinstance(cls, type) and issubclass(cls, errors.RPCError):
        if payload["seconds"] is not None:
            raise cls(request=None, capture=payload["seconds"])
        try:
            error = cls(request=None)
        except TypeError:
            raise TelegramOwnerRemoteError(name) from None
        raise error
    raise TelegramOwnerRemoteError(name)
