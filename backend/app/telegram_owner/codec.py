"""Closed JSON vocabulary for private gateway IPC; never deserialize code."""
from __future__ import annotations

import base64
import dataclasses
import json
from datetime import datetime
from functools import lru_cache


def dumps(value: object) -> bytes:
    return json.dumps(_encode(value), separators=(",", ":"), allow_nan=False).encode()


def loads(value: bytes):
    return _decode(json.loads(value))


@lru_cache(maxsize=1)
def _contracts() -> dict[str, type]:
    from app.integrations.telegram import contracts, message_observation, update_contracts
    from app.integrations.telegram.search_join import (
        ImageVerificationRequest, ImageVerificationDecision, ImageVerificationVote,
    )

    modules = (contracts, message_observation, update_contracts)
    known = {
        name: value for module in modules for name, value in vars(module).items()
        if isinstance(value, type) and dataclasses.is_dataclass(value)
    }
    return {**known, **{cls.__name__: cls for cls in (
        ImageVerificationRequest, ImageVerificationDecision, ImageVerificationVote,
    )}}


def _encode(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        return {"type": "dict", "items": [[_encode(k), _encode(v)] for k, v in value.items()]}
    if dataclasses.is_dataclass(value) and type(value).__name__ in _contracts():
        fields = {field.name: _encode(getattr(value, field.name)) for field in dataclasses.fields(value)}
        return {"type": "contract", "name": type(value).__name__, "fields": fields}
    return _encode_extended(value)


def _encode_extended(value):
    if isinstance(value, bytes):
        return {"type": "bytes", "value": base64.b64encode(value).decode()}
    if isinstance(value, datetime):
        return {"type": "datetime", "value": value.isoformat()}
    if isinstance(value, (tuple, set, frozenset)):
        return {"type": type(value).__name__, "items": [_encode(item) for item in value]}
    raise TypeError("telegram_owner_unsupported_value_type:" + type(value).__name__)


def _decode(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [_decode(item) for item in value]
    kind = value["type"]
    if kind == "dict":
        return {_decode(k): _decode(v) for k, v in value["items"]}
    if kind == "contract":
        cls = _contracts()[value["name"]]
        return cls(**{key: _decode(item) for key, item in value["fields"].items()})
    return _decode_extended(kind, value)


def _decode_extended(kind, value):
    if kind == "bytes":
        return base64.b64decode(value["value"], validate=True)
    if kind == "datetime":
        return datetime.fromisoformat(value["value"])
    constructors = {"tuple": tuple, "set": set, "frozenset": frozenset}
    if kind in constructors:
        return constructors[kind](_decode(item) for item in value["items"])
    raise ValueError("telegram_owner_unsupported_wire_type")
