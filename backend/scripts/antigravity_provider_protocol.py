from __future__ import annotations

import json
import time
from http import HTTPStatus
from typing import Any


USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "thinking_tokens",
    "cache_read_tokens",
    "total_tokens",
)
PRE_CALL_FAILURE_CODES = frozenset({
    "antigravity_auth_required",
    "antigravity_account_ineligible",
    "antigravity_model_invalid",
    "antigravity_quota_limited",
})


class BridgeError(RuntimeError):
    def __init__(self, code: str, status: int, *, state: str = "not_started") -> None:
        self.code = code
        self.status = status
        self.state = state
        super().__init__(code)


def parse_cli_output(
    request_id: str,
    payload: dict[str, Any],
    completed,
    started: float,
) -> dict[str, Any]:
    envelope = _load_optional_object(completed.stdout)
    if completed.returncode != 0:
        _raise_classified_failure(envelope, completed.stderr)
    if not envelope:
        raise BridgeError(
            "antigravity_invalid_envelope", HTTPStatus.UNPROCESSABLE_ENTITY, state="failed",
        )
    if envelope.get("status") != "SUCCESS":
        _raise_classified_failure(envelope, completed.stderr)
    structured = envelope.get("structured_output")
    if not isinstance(structured, dict):
        raise BridgeError(
            "antigravity_schema_missing", HTTPStatus.UNPROCESSABLE_ENTITY, state="failed",
        )
    usage = dict(envelope.get("usage") or {})
    return {
        "request_id": request_id,
        "state": "confirmed",
        "model": str(payload["model"]),
        "structured_output": structured,
        "usage": {field: int(usage.get(field) or 0) for field in USAGE_FIELDS},
        "duration_seconds": round(time.monotonic() - started, 3),
        "num_turns": int(envelope.get("num_turns") or 0),
    }


def _raise_classified_failure(envelope: dict[str, Any], stderr: str) -> None:
    code = _classify_failure(envelope, stderr)
    if code in PRE_CALL_FAILURE_CODES and not _proven_pre_call(envelope):
        raise BridgeError(code, HTTPStatus.ACCEPTED, state="unknown")
    state = "not_started" if code in PRE_CALL_FAILURE_CODES else "failed"
    raise BridgeError(code, HTTPStatus.UNPROCESSABLE_ENTITY, state=state)


def _load_optional_object(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _classify_failure(envelope: dict[str, Any], stderr: str) -> str:
    detail = " ".join((str(envelope.get("error") or ""), stderr)).lower()
    if "not eligible" in detail or "eligibility check failed" in detail:
        return "antigravity_account_ineligible"
    if "authentication" in detail or "sign in" in detail or "log in" in detail:
        return "antigravity_auth_required"
    if "quota" in detail or "rate limit" in detail or "credits" in detail:
        return "antigravity_quota_limited"
    if "model" in detail and ("invalid" in detail or "not recognized" in detail):
        return "antigravity_model_invalid"
    return "antigravity_cli_exit_nonzero"


def _proven_pre_call(envelope: dict[str, Any]) -> bool:
    if int(envelope.get("num_turns") or 0) != 0:
        return False
    usage = envelope.get("usage")
    if not isinstance(usage, dict):
        return False
    return all(int(usage.get(field) or 0) == 0 for field in USAGE_FIELDS)


__all__ = ["BridgeError", "parse_cli_output"]
