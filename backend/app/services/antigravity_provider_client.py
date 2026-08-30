from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any

from app.ai_gateway import AiProviderCredentials, AiUsage


ANTIGRAVITY_PROVIDER_TYPE = "antigravity_cli"
ANTIGRAVITY_PRIMARY_MODEL = "gemini-3.5-flash-medium"
ANTIGRAVITY_SECONDARY_MODEL = "gemini-3.1-pro-low"
ANTIGRAVITY_MODELS = frozenset(
    {ANTIGRAVITY_PRIMARY_MODEL, ANTIGRAVITY_SECONDARY_MODEL}
)


class AntigravityProviderResultUnknown(RuntimeError):
    pass


class AntigravityProviderPreCallError(ConnectionError):
    pass


@dataclass(frozen=True)
class AntigravityResponse:
    payload: object
    usage: AiUsage


class AntigravityProviderClient:
    def generate(
        self,
        credentials: AiProviderCredentials,
        *,
        request_id: str,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict[str, Any],
        timeout: float,
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
            timeout=timeout + 20,
        )
        state = str(data.get("state") or "")
        if state in {"started", "unknown"}:
            raise AntigravityProviderResultUnknown(
                "antigravity_provider_result_unknown"
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
        if response.payload != {"status": "ok"}:
            return False, "antigravity_schema_invalid"
        return True, "antigravity process/auth/model/schema ready"

    def _request(
        self,
        credentials: AiProviderCredentials,
        path: str,
        *,
        method: str,
        payload: dict[str, Any] | None,
        timeout: float,
    ) -> dict[str, Any]:
        url = f"{credentials.base_url.rstrip('/')}{path}"
        headers = {
            "Authorization": f"Bearer {credentials.api_key}",
            "Content-Type": "application/json",
        }
        raw = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=raw, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return self._load_object(response.read())
        except urllib.error.HTTPError as exc:
            data = self._load_error(exc)
            state = str(data.get("state") or "")
            code = str(data.get("error_code") or f"antigravity_bridge_http_{exc.code}")
            if state in {"started", "unknown"} or exc.code == 202:
                raise AntigravityProviderResultUnknown(code) from exc
            if exc.code in {401, 409, 422}:
                raise RuntimeError(code) from exc
            raise AntigravityProviderPreCallError(code) from exc
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
            raise RuntimeError("antigravity_model_invalid")

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
