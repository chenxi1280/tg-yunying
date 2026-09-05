from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from dataclasses import replace
from typing import Any

from app.ai_gateway import AiProviderCredentials, AiUsage
from app.ai_transport_errors import AiProviderResultUnknown
from app.ai_http_transport import hard_deadline_options, read_http


ANTIGRAVITY_PROVIDER_TYPE = "antigravity_cli"
LEGACY_HTTP_SLACK_SECONDS = 20
ANTIGRAVITY_PRIMARY_MODEL = "gemini-3.6-flash-medium"
ANTIGRAVITY_SECONDARY_MODEL = "gemini-3.1-pro-low"
ANTIGRAVITY_MODELS = frozenset(
    {ANTIGRAVITY_PRIMARY_MODEL, ANTIGRAVITY_SECONDARY_MODEL}
)
ANTIGRAVITY_PRE_CALL_CODES = frozenset({
    "antigravity_auth_required",
    "antigravity_account_ineligible",
    "antigravity_model_invalid",
    "antigravity_quota_limited",
    "antigravity_capacity_busy",
    "antigravity_binary_missing",
    "antigravity_process_start_failed",
})


class AntigravityProviderResultUnknown(AiProviderResultUnknown):
    pass


class AntigravityProviderPreCallError(ConnectionError):
    pass


@dataclass(frozen=True)
class AntigravityResponse:
    payload: object
    usage: AiUsage


class AntigravityProviderClient:
    def __init__(self, *, http_transport=None):
        self._http_transport = http_transport

    def generate(
        self,
        credentials: AiProviderCredentials,
        *,
        request_id: str,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict[str, Any],
        timeout: float,
        request_deadline: float | None = None,
    ) -> AntigravityResponse:
        self._validate_model(credentials.model_name)
        body = {
            "request_id": request_id or str(uuid.uuid4()),
            "model": credentials.model_name,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "json_schema": json_schema,
            "timeout_seconds": int(timeout),
        }
        data = self._request(
            credentials,
            "/internal/v1/generate",
            method="POST",
            payload=body,
            timeout=timeout + LEGACY_HTTP_SLACK_SECONDS if request_deadline is None else timeout,
            **hard_deadline_options(request_deadline),
        )
        state = str(data.get("state") or "")
        if state in {"started", "unknown"}:
            raise AntigravityProviderResultUnknown(
                str(data.get("error_code") or "antigravity_provider_result_unknown")
            )
        if state != "confirmed":
            code = str(data.get("error_code") or "antigravity_cli_exit_nonzero")
            raise RuntimeError(code)
        return AntigravityResponse(
            payload=data.get("structured_output"),
            usage=self._usage(data.get("usage")),
        )

    def check(
        self,
        credentials: AiProviderCredentials,
        *,
        timeout: float,
    ) -> tuple[bool, str]:
        for model in sorted(ANTIGRAVITY_MODELS):
            if not self._check_model(replace(credentials, model_name=model), timeout):
                return False, f"antigravity_schema_invalid:{model}"
        health = self._request(
            credentials,
            "/internal/v1/health",
            method="GET",
            payload=None,
            timeout=timeout,
        )
        return self._validate_health(health)

    def _check_model(
        self,
        credentials: AiProviderCredentials,
        timeout: float,
    ) -> bool:
        schema = {
            "type": "object",
            "properties": {"status": {"type": "string", "enum": ["ok"]}},
            "required": ["status"],
            "additionalProperties": False,
        }
        response = self.generate(
            credentials,
            request_id=f"health-{uuid.uuid4()}",
            system_prompt="Return only the requested structured health result.",
            user_prompt="Do not use tools. Return status ok.",
            json_schema=schema,
            timeout=timeout,
        )
        return response.payload == {"status": "ok"}

    def _validate_health(self, health: dict[str, Any]) -> tuple[bool, str]:
        visibility = dict(health.get("model_visibility") or {})
        expected_models = set(ANTIGRAVITY_MODELS)
        probe_age = health.get("schema_probe_age_seconds")
        max_age = health.get("probe_max_age_seconds")
        ready = (
            health.get("status") == "ready"
            and health.get("binary_ready") is True
            and str(health.get("cli_version") or "") not in {"", "missing", "unavailable"}
            and health.get("process_state") == "idle"
            and health.get("last_terminal_code") == "confirmed"
            and health.get("quota_limited") is False
            and expected_models == {model for model, visible in visibility.items() if visible}
            and isinstance(probe_age, (int, float))
            and isinstance(max_age, (int, float))
            and probe_age <= max_age
        )
        if not ready:
            return False, f"antigravity_health_not_ready:{health}"
        return True, "antigravity process/auth/models/schema/health ready"

    def _request(
        self,
        credentials: AiProviderCredentials,
        path: str,
        *,
        method: str,
        payload: dict[str, Any] | None,
        timeout: float,
        request_deadline: float | None = None,
    ) -> dict[str, Any]:
        url = f"{credentials.base_url.rstrip('/')}{path}"
        headers = {
            "Authorization": f"Bearer {credentials.api_key}",
            "Content-Type": "application/json",
        }
        raw = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=raw, headers=headers, method=method)
        try:
            transport = self._http_transport or read_http
            return self._load_object(transport(request, timeout=timeout, request_deadline=request_deadline))
        except urllib.error.HTTPError as exc:
            data = self._load_error(exc)
            state = str(data.get("state") or "")
            code = str(data.get("error_code") or f"antigravity_bridge_http_{exc.code}")
            if state in {"started", "unknown"} or exc.code == 202:
                raise AntigravityProviderResultUnknown(code) from exc
            if state == "not_started" and code in ANTIGRAVITY_PRE_CALL_CODES:
                raise AntigravityProviderPreCallError(code) from exc
            if exc.code >= 500:
                raise AntigravityProviderResultUnknown(
                    "antigravity_provider_result_unknown"
                ) from exc
            raise RuntimeError(code) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise AntigravityProviderResultUnknown(
                "antigravity_provider_result_unknown"
            ) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise AntigravityProviderResultUnknown(
                    "antigravity_provider_result_unknown"
                ) from exc
            raise AntigravityProviderPreCallError(
                "antigravity_bridge_unreachable_pre_call"
            ) from exc

    def _validate_model(self, model: str) -> None:
        if model not in ANTIGRAVITY_MODELS:
            raise AntigravityProviderPreCallError("antigravity_model_invalid")

    def _load_error(self, error: urllib.error.HTTPError) -> dict[str, Any]:
        try:
            return self._load_object(error.read())
        except (RuntimeError, json.JSONDecodeError):
            return {}

    def _load_object(self, raw: bytes) -> dict[str, Any]:
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError("antigravity_invalid_envelope")
        return data

    def _usage(self, raw: object) -> AiUsage:
        values = raw if isinstance(raw, dict) else {}
        return AiUsage(
            prompt_tokens=int(values.get("input_tokens") or 0),
            completion_tokens=int(values.get("output_tokens") or 0),
            cached_tokens=int(values.get("cache_read_tokens") or 0),
            total_tokens=int(values.get("total_tokens") or 0),
            billable=False,
        )


__all__ = [
    "ANTIGRAVITY_PRIMARY_MODEL",
    "ANTIGRAVITY_PROVIDER_TYPE",
    "ANTIGRAVITY_SECONDARY_MODEL",
    "AntigravityProviderClient",
    "AntigravityProviderPreCallError",
    "AntigravityProviderResultUnknown",
]
