from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AiProvider, AiProviderHealthStatus, TgAccount
from app.services._common import ai_gateway
from app.services.ai_config import get_tenant_ai_setting

GENERIC_SUMMARY_TERMS = {"自然", "随意", "真实", "像真人"}
VOICE_PROFILE_AI_TIMEOUT_SECONDS = 45
VOICE_PROFILE_INITIAL_MAX_TOKENS = 768
VOICE_PROFILE_RETRY_MAX_TOKENS = 2048
ACTIONABLE_LIST_FIELDS = ("interaction_habits", "forbidden_expressions")
MIN_ACTIONABLE_LIST_ITEMS = 3
MAX_ACTIONABLE_LIST_ITEMS = 5


def _valid_summary(profile: dict[str, Any], account_id: int) -> str:
    _validate_generated_profile(profile, account_id)
    summary = str(profile.get("short_prompt_summary") or "").strip()
    return summary


def _voice_profile_ai_provider(session: Session, tenant_id: int) -> tuple[AiProvider, Any]:
    setting = get_tenant_ai_setting(session, tenant_id)
    provider = session.get(AiProvider, setting.default_provider_id) if setting.default_provider_id else None
    if not _provider_usable(provider):
        provider = session.scalar(
            select(AiProvider)
            .where(AiProvider.is_active.is_(True), AiProvider.health_status == AiProviderHealthStatus.HEALTHY.value)
            .order_by(AiProvider.id.asc())
        )
    if not _provider_usable(provider):
        raise RuntimeError("账号表达卡重建需要健康可用的 AI 供应商")
    return provider, setting


def _generate_voice_profile_payloads(session: Session, tenant_id: int, account_ids: list[int], credentials, setting) -> list[dict[str, Any]]:
    accounts = _accounts_for_generation(session, tenant_id, account_ids)
    profiles = _request_voice_profile_payloads(credentials, setting, accounts)
    account_by_id = {account.id: account for account in accounts}
    missing_ids = [account.id for account in accounts if account.id not in profiles]
    for account_id in missing_ids:
        profiles.update(_request_voice_profile_payloads(credentials, setting, [account_by_id[account_id]]))
    final_missing = [account.id for account in accounts if account.id not in profiles]
    if final_missing:
        raise RuntimeError(f"AI 表达卡缺少账号: {final_missing}")
    return [_normalize_generated_profile(profiles[account.id]) for account in accounts]


def _parse_voice_profile_payloads(raw: str, expected_account_ids: list[int]) -> list[dict[str, Any]]:
    profiles = _parse_voice_profile_payload_map(raw)
    missing = [account_id for account_id in expected_account_ids if account_id not in profiles]
    if missing:
        raise RuntimeError(f"AI 表达卡缺少账号: {missing}")
    return [_normalize_generated_profile(profiles[account_id]) for account_id in expected_account_ids]


def _validate_summary(summary: str, account_id: int) -> None:
    if not summary:
        raise ValueError(f"voice profile summary missing for account {account_id}")
    generic_hits = sum(1 for term in GENERIC_SUMMARY_TERMS if term in summary)
    if generic_hits >= 2:
        raise ValueError(f"voice profile summary too generic for account {account_id}")


def _validate_generated_profile(profile: dict[str, Any], account_id: int) -> None:
    summary = str(profile.get("short_prompt_summary") or "").strip()
    _validate_summary(summary, account_id)
    for field in ACTIONABLE_LIST_FIELDS:
        _validate_actionable_list(field, profile.get(field), account_id)


def _validate_actionable_list(field: str, value: Any, account_id: int) -> None:
    items = _string_list(value)
    if MIN_ACTIONABLE_LIST_ITEMS <= len(items) <= MAX_ACTIONABLE_LIST_ITEMS:
        return
    raise ValueError(
        f"voice profile {field} requires {MIN_ACTIONABLE_LIST_ITEMS}-{MAX_ACTIONABLE_LIST_ITEMS} items for account {account_id}"
    )


def _provider_usable(provider: AiProvider | None) -> bool:
    return bool(provider and provider.is_active and provider.health_status == AiProviderHealthStatus.HEALTHY.value)


def _request_voice_profile_payloads(credentials, setting, accounts: list[TgAccount]) -> dict[int, dict[str, Any]]:
    raw, _usage = ai_gateway._post_openai_compatible(  # noqa: SLF001 - project adapter has no public JSON generation API yet.
        credentials,
        _voice_profile_prompt(accounts),
        setting.temperature,
        VOICE_PROFILE_INITIAL_MAX_TOKENS,
        system_prompt="你是账号表达卡生成器，只输出指定格式的纯文本行，不解释。",
        response_format_json=False,
        reasoning_retry_max_tokens=VOICE_PROFILE_RETRY_MAX_TOKENS,
        timeout=VOICE_PROFILE_AI_TIMEOUT_SECONDS,
    )
    try:
        return _parse_voice_profile_payload_map(raw)
    except (json.JSONDecodeError, RuntimeError) as exc:
        if len(accounts) <= 1 or not _is_voice_profile_structure_error(exc):
            raise
        return _request_voice_profile_payloads_individually(credentials, setting, accounts)


def _request_voice_profile_payloads_individually(credentials, setting, accounts: list[TgAccount]) -> dict[int, dict[str, Any]]:
    profiles: dict[int, dict[str, Any]] = {}
    for account in accounts:
        profiles.update(_request_voice_profile_payloads(credentials, setting, [account]))
    return profiles


def _is_voice_profile_structure_error(exc: BaseException) -> bool:
    if isinstance(exc, json.JSONDecodeError):
        return True
    if not isinstance(exc, RuntimeError):
        return False
    return any(fragment in str(exc) for fragment in ("输出为空", "字段数量错误", "JSON 行不是对象"))


def _accounts_for_generation(session: Session, tenant_id: int, account_ids: list[int]) -> list[TgAccount]:
    accounts = list(
        session.scalars(
            select(TgAccount).where(
                TgAccount.tenant_id == tenant_id,
                TgAccount.id.in_(account_ids),
            )
        )
    )
    account_by_id = {account.id: account for account in accounts}
    missing = [account_id for account_id in account_ids if account_id not in account_by_id]
    if missing:
        raise ValueError(f"账号不存在或不属于当前租户: {missing[0]}")
    return [account_by_id[account_id] for account_id in account_ids]


def _voice_profile_prompt(accounts: list[TgAccount]) -> str:
    account_lines = "\n".join(f"- account_id={item.id}, name={item.display_name}, username={item.username or '-'}" for item in accounts)
    return (
        f"为以下 {len(accounts)} 个 Telegram 运营账号生成互相差异明显的账号表达卡。\n{account_lines}\n"
        "每个账号只输出一行 JSON，不要输出数组、标题、解释、Markdown。\n"
        "每行字段固定为：id,age,px,cx,len,habits,tone,words,emoji,ban,summary。\n"
        "px/cx/words 必须是短字符串数组；habits 和 ban 必须各写 3-5 条短句。\n"
        "summary 必须具体可执行，写成 18-36 个汉字。\n"
        "禁止只写自然、随意、真实；同批账号句长、口头习惯、互动偏好、表情倾向要明显不同。"
    )


def _parse_voice_profile_payload_map(raw: str) -> dict[int, dict[str, Any]]:
    lines = [line.strip() for line in _clean_profile_lines(raw).splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("AI 表达卡输出为空")
    items = [_profile_from_line(line) for line in lines]
    return {int(item["account_id"]): item for item in items if item.get("account_id") is not None}


def _clean_profile_lines(raw: str) -> str:
    value = str(raw or "").strip()
    if value.startswith("```"):
        value = value.strip("`").removeprefix("jsonl").removeprefix("json").removeprefix("text").strip()
    return value


def _profile_from_line(line: str) -> dict[str, Any]:
    return _profile_from_json_line(line) if line.startswith("{") else _profile_from_pipe_line(line)


def _profile_from_json_line(line: str) -> dict[str, Any]:
    payload = json.loads(line)
    if not isinstance(payload, dict):
        raise RuntimeError("AI 表达卡 JSON 行不是对象")
    return {
        "account_id": payload.get("id"),
        "age_band": payload.get("age"),
        "persona_experiences": payload.get("px"),
        "consumption_experiences": payload.get("cx"),
        "sentence_length": payload.get("len"),
        "interaction_habits": payload.get("habits"),
        "tone_strength": payload.get("tone"),
        "lexical_preferences": payload.get("words"),
        "emoji_policy": payload.get("emoji"),
        "forbidden_expressions": payload.get("ban"),
        "short_prompt_summary": payload.get("summary"),
    }


def _profile_from_pipe_line(line: str) -> dict[str, Any]:
    parts = [part.strip() for part in line.split("|")]
    if len(parts) != 11:
        raise RuntimeError("AI 表达卡输出行字段数量错误")
    return {
        "account_id": parts[0],
        "age_band": parts[1],
        "persona_experiences": _semicolon_list(parts[2]),
        "consumption_experiences": _semicolon_list(parts[3]),
        "sentence_length": parts[4],
        "interaction_habits": _semicolon_list(parts[5]),
        "tone_strength": parts[6],
        "lexical_preferences": _semicolon_list(parts[7]),
        "emoji_policy": parts[8],
        "forbidden_expressions": _semicolon_list(parts[9]),
        "short_prompt_summary": parts[10],
    }


def _semicolon_list(value: str) -> list[str]:
    return [item.strip() for item in value.replace("；", ";").split(";") if item.strip()]


def _normalize_generated_profile(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise RuntimeError("AI 表达卡输出项不是对象")
    result = dict(item)
    for key in ("persona_experiences", "consumption_experiences", "interaction_habits", "lexical_preferences", "forbidden_expressions"):
        result[key] = _string_list(result.get(key))
    _validate_generated_profile(result, int(result.get("account_id") or 0))
    return result


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


__all__ = [
    "VOICE_PROFILE_INITIAL_MAX_TOKENS",
    "VOICE_PROFILE_RETRY_MAX_TOKENS",
    "_generate_voice_profile_payloads",
    "_parse_voice_profile_payloads",
    "_valid_summary",
    "_validate_summary",
    "_voice_profile_ai_provider",
]
