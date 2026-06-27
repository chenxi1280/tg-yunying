from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


TELEGRAM_API_BASE = "https://api.telegram.org"


@dataclass(frozen=True)
class TelegramBotApiResult:
    ok: bool
    detail: str = ""
    data: dict[str, Any] | None = None


def set_telegram_webhook(bot_token: str, webhook_url: str) -> TelegramBotApiResult:
    return _post_form(bot_token, "setWebhook", {"url": webhook_url})


def get_telegram_webhook_info(bot_token: str) -> TelegramBotApiResult:
    return _post_form(bot_token, "getWebhookInfo", {})


def delete_telegram_webhook(bot_token: str) -> TelegramBotApiResult:
    return _post_form(bot_token, "deleteWebhook", {})


def _post_form(bot_token: str, method: str, payload: dict[str, str]) -> TelegramBotApiResult:
    request = urllib.request.Request(
        f"{TELEGRAM_API_BASE}/bot{bot_token}/{method}",
        data=urllib.parse.urlencode(payload).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return _telegram_result(response.read().decode("utf-8", errors="ignore"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        return TelegramBotApiResult(False, f"Telegram Bot HTTP {exc.code}: {detail[:300]}", {})
    except Exception as exc:  # noqa: BLE001 - caller persists visible failure state.
        return TelegramBotApiResult(False, str(exc), {})


def _telegram_result(body: str) -> TelegramBotApiResult:
    try:
        data = json.loads(body or "{}")
    except json.JSONDecodeError as exc:
        return TelegramBotApiResult(False, f"Telegram Bot returned invalid JSON: {exc}", {})
    result = data.get("result")
    result_data = result if isinstance(result, dict) else {}
    if data.get("ok"):
        return TelegramBotApiResult(True, "ok", result_data)
    return TelegramBotApiResult(False, str(data)[:300], result_data)


__all__ = [
    "TelegramBotApiResult",
    "delete_telegram_webhook",
    "get_telegram_webhook_info",
    "set_telegram_webhook",
]
