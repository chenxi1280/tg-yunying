"""Telegram's account freeze contract, independent of target permissions."""
from datetime import datetime, timezone
import math

from telethon import functions, types

from .contracts import AccountHealth

FROZEN_HEALTH_SCORE = 20
HEALTHY_SCORE = 95
FROZEN_ERROR_CODE = "account_frozen"
FROZEN_DETAIL = "Telegram account frozen: business operations are unavailable"
FROZEN_MARKERS = (
    "frozen_method_invalid", "frozen_participant_missing",
    "frozenmethodinvalid", "frozenparticipantmissing",
    "frozen account", "frozen accounts", "account frozen", "account_frozen",
)


def is_account_frozen_error(*parts: object) -> bool:
    detail = " ".join(str(part or "") for part in parts).lower()
    return any(marker in detail for marker in FROZEN_MARKERS)


async def read_account_freeze_health(client) -> AccountHealth:
    observed_at = datetime.now(timezone.utc)
    response = await client(functions.help.GetAppConfigRequest(hash=0))
    if not isinstance(response, types.help.AppConfig):
        raise RuntimeError("telegram_freeze_check_requires_full_app_config")
    frozen = _frozen_from_config(response.config)
    return AccountHealth(
        status="疑似封禁" if frozen else "在线",
        health_score=FROZEN_HEALTH_SCORE if frozen else HEALTHY_SCORE,
        detail=FROZEN_DETAIL if frozen else "账号 session 可用，Telegram 冻结检查通过",
        telegram_frozen=frozen,
        freeze_observed_at=observed_at,
    )


def _frozen_from_config(config) -> bool:
    if not isinstance(config, types.JsonObject):
        raise RuntimeError("telegram_freeze_check_invalid_config")
    entries = [entry.value for entry in config.value if entry.key == "freeze_since_date"]
    if not entries:
        return False
    if len(entries) != 1 or not isinstance(entries[0], types.JsonNumber):
        raise RuntimeError("telegram_freeze_check_invalid_since_date")
    value = entries[0].value
    if not math.isfinite(value) or value < 0 or value != int(value):
        raise RuntimeError("telegram_freeze_check_invalid_since_date")
    return value > 0
