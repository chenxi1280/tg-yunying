from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid


MODELS = ("gemini-3.6-flash-medium", "gemini-3.1-pro-low")
STARTUP_ATTEMPTS = 10
STARTUP_INTERVAL_SECONDS = 1
CALL_TIMEOUT_SECONDS = 180


def main() -> None:
    base_url = os.environ["ANTIGRAVITY_BRIDGE_URL"].rstrip("/")
    token = os.environ["ANTIGRAVITY_BRIDGE_TOKEN"]
    release_sha = os.environ["RELEASE_SHA"]
    for model in MODELS:
        _probe_model(base_url, token, release_sha, model)
    health = _request(base_url, token, "/internal/v1/health")
    if health.get("status") != "ready":
        raise RuntimeError(f"antigravity_slot_health_not_ready:{health}")
    if set(health.get("confirmed_models") or ()) != set(MODELS):
        raise RuntimeError("antigravity_slot_model_visibility_incomplete")
    print(json.dumps({
        "status": "ready",
        "bridge_version": health.get("bridge_version"),
        "slot_id": health.get("slot_id"),
        "confirmed_models": health.get("confirmed_models"),
        "release_sha": release_sha,
    }, sort_keys=True))


def _probe_model(base_url: str, token: str, release_sha: str, model: str) -> None:
    payload = {
        "request_id": f"release-health:{release_sha}:{uuid.uuid4()}:{model}",
        "model": model,
        "system_prompt": "Return only the requested structured health result.",
        "user_prompt": "Do not use tools. Return status ok.",
        "json_schema": {
            "type": "object",
            "properties": {"status": {"type": "string", "enum": ["ok"]}},
            "required": ["status"],
            "additionalProperties": False,
        },
        "timeout_seconds": 120,
    }
    response = _request(base_url, token, "/internal/v1/generate", payload)
    if response.get("state") != "confirmed" or response.get("structured_output") != {"status": "ok"}:
        raise RuntimeError(f"antigravity_slot_probe_failed:{model}:{response}")


def _request(
    base_url: str,
    token: str,
    path: str,
    payload: dict | None = None,
) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    for attempt in range(1, STARTUP_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=CALL_TIMEOUT_SECONDS) as response:
                result = json.loads(response.read())
                if not isinstance(result, dict):
                    raise RuntimeError("antigravity_slot_invalid_response")
                return result
        except urllib.error.URLError:
            if attempt == STARTUP_ATTEMPTS:
                raise
            time.sleep(STARTUP_INTERVAL_SECONDS)
    raise RuntimeError("antigravity_slot_probe_unreachable")


if __name__ == "__main__":
    main()
