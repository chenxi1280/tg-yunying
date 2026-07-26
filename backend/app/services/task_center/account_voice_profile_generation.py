from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AiProvider, AiProviderHealthStatus, TgAccount
from app.services._common import ai_gateway
from app.services.ai_config import ai_provider_credentials, get_tenant_ai_setting

GENERIC_SUMMARY_TERMS = {"自然", "随意", "真实", "像真人"}
VOICE_PROFILE_AI_TIMEOUT_SECONDS = 45
VOICE_PROFILE_INITIAL_MAX_TOKENS = 512
ACTIONABLE_LIST_FIELDS = ("interaction_habits", "forbidden_expressions")
MIN_ACTIONABLE_LIST_ITEMS = 3
MAX_ACTIONABLE_LIST_ITEMS = 5
MIN_LIGHTWEIGHT_SUMMARY_LENGTH = 18
MAX_LIGHTWEIGHT_SUMMARY_LENGTH = 36
MALE_MASK_TERMS = ("男", "男性", "男人", "男生", "男士", "老哥", "大哥", "老板", "先生")
RESTRICTED_LIGHTWEIGHT_MASK_TERMS = ("色情", "性交易", "寻欢", "夜场", "楼凤", "外围", "招嫖", "嫖客")
STRUCTURED_MASK_FIELDS = ("mask_name", "audience_archetype", "identity_frame")


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
        raise RuntimeError("账号面具重建需要健康可用的 AI 供应商")
    return provider, setting


def _generate_voice_profile_payloads(
    session: Session,
    tenant_id: int,
    account_ids: list[int],
    credentials,
    setting,
    *,
    strict_lightweight: bool = False,
) -> list[dict[str, Any]]:
    accounts = _accounts_for_generation(session, tenant_id, account_ids)
    profiles = _request_voice_profile_payloads(credentials, setting, accounts, strict_lightweight=strict_lightweight)
    missing_ids = [account.id for account in accounts if account.id not in profiles]
    if missing_ids:
        raise RuntimeError(f"AI 面具缺少账号: {missing_ids}")
    return [_normalize_generated_profile(profiles[account.id], strict_lightweight=strict_lightweight) for account in accounts]


def generate_lightweight_voice_profile_payloads(
    session: Session,
    *,
    tenant_id: int,
    account_ids: list[int],
) -> list[dict[str, Any]]:
    provider, setting = _voice_profile_ai_provider(session, tenant_id)
    credentials = ai_provider_credentials(provider)
    if credentials.base_url.startswith("mock://"):
        raise RuntimeError("账号面具重建需要真实 AI 供应商，当前供应商为 mock")
    return _generate_voice_profile_payloads(
        session,
        tenant_id,
        account_ids,
        credentials,
        setting,
        strict_lightweight=True,
    )


def _parse_voice_profile_payloads(
    raw: str,
    expected_account_ids: list[int],
    *,
    strict_lightweight: bool = False,
) -> list[dict[str, Any]]:
    profiles = _parse_voice_profile_payload_map(raw, strict_lightweight=strict_lightweight)
    missing = [account_id for account_id in expected_account_ids if account_id not in profiles]
    if missing:
        raise RuntimeError(f"AI 面具缺少账号: {missing}")
    return [_normalize_generated_profile(profiles[account_id], strict_lightweight=strict_lightweight) for account_id in expected_account_ids]


def _validate_summary(summary: str, account_id: int) -> None:
    if not summary:
        raise ValueError(f"voice profile summary missing for account {account_id}")
    generic_hits = sum(1 for term in GENERIC_SUMMARY_TERMS if term in summary)
    if generic_hits >= 2:
        raise ValueError(f"voice profile summary too generic for account {account_id}")


def _validate_generated_profile(profile: dict[str, Any], account_id: int) -> None:
    summary = str(profile.get("short_prompt_summary") or "").strip()
    _validate_summary(summary, account_id)
    _validate_male_mask(profile, account_id)
    for field in ACTIONABLE_LIST_FIELDS:
        _validate_actionable_list(field, profile.get(field), account_id)


def _validate_male_mask(profile: dict[str, Any], account_id: int) -> None:
    structured_values = [str(profile.get(field) or "").strip() for field in STRUCTURED_MASK_FIELDS]
    if not any(structured_values):
        return
    identity_text = " ".join([*structured_values, str(profile.get("short_prompt_summary") or "")])
    if any(term in identity_text for term in MALE_MASK_TERMS):
        return
    raise ValueError(f"account mask gender must be male for account {account_id}")


def _validate_actionable_list(field: str, value: Any, account_id: int) -> None:
    items = _string_list(value)
    if MIN_ACTIONABLE_LIST_ITEMS <= len(items) <= MAX_ACTIONABLE_LIST_ITEMS:
        return
    raise ValueError(
        f"voice profile {field} requires {MIN_ACTIONABLE_LIST_ITEMS}-{MAX_ACTIONABLE_LIST_ITEMS} items for account {account_id}"
    )


def _provider_usable(provider: AiProvider | None) -> bool:
    return bool(provider and provider.is_active and provider.health_status == AiProviderHealthStatus.HEALTHY.value)


def _request_voice_profile_payloads(
    credentials,
    setting,
    accounts: list[TgAccount],
    *,
    strict_lightweight: bool = False,
) -> dict[int, dict[str, Any]]:
    raw, _usage = ai_gateway._post_openai_compatible(  # noqa: SLF001 - project adapter has no public JSON generation API yet.
        credentials,
        _voice_profile_prompt(accounts),
        setting.temperature,
        VOICE_PROFILE_INITIAL_MAX_TOKENS,
        system_prompt="你是轻量账号面具生成器，只输出指定格式的单行 JSON，不解释。",
        response_format_json=strict_lightweight,
        timeout=VOICE_PROFILE_AI_TIMEOUT_SECONDS,
    )
    return _parse_voice_profile_payload_map(raw, strict_lightweight=strict_lightweight)


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
        f"为以下 {len(accounts)} 个 Telegram 运营账号生成互相差异明显的轻量独立账号面具。\n{account_lines}\n"
        "每个账号只输出一行 JSON，不要输出数组、标题、解释、Markdown 或额外字段。\n"
        "每行字段固定为：id,mask,aud,frame,tags,habits,ban,summary。\n"
        "mask 是 6-12 字面具名；aud 是目标受众原型；frame 是账号身份框架；tags 是 2-4 个偏好标签。\n"
        "所有账号面具必须体现成年男性日常社交身份，mask/aud/frame/summary 至少一处写男性、男生、男士、老哥、大哥、老板或先生；不得生成女性或中性身份。\n"
        "habits 和 ban 必须各写 3-5 条短句；summary 必须具体可执行，写成 18-36 个汉字。\n"
        "只写日常话题、兴趣和表达习惯，不写成人、交易或违规暗示；禁止冒充真实用户、管理员或指定个人；不得复用其他账号的面具内容。"
    )


def _parse_voice_profile_payload_map(raw: str, *, strict_lightweight: bool = False) -> dict[int, dict[str, Any]]:
    lines = [line.strip() for line in _clean_profile_lines(raw).splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("AI 面具输出为空")
    items = [_profile_from_line(line, strict_lightweight=strict_lightweight) for line in lines]
    return {int(item["account_id"]): item for item in items if item.get("account_id") is not None}


def _clean_profile_lines(raw: str) -> str:
    value = str(raw or "").strip()
    if value.startswith("```"):
        value = value.strip("`").removeprefix("jsonl").removeprefix("json").removeprefix("text").strip()
    return value


def _profile_from_line(line: str, *, strict_lightweight: bool) -> dict[str, Any]:
    if line.startswith("{"):
        return _profile_from_json_line(line)
    if strict_lightweight:
        raise RuntimeError("轻量账号面具输出必须是 JSON 行")
    return _profile_from_pipe_line(line)


def _profile_from_json_line(line: str) -> dict[str, Any]:
    payload = json.loads(line)
    if not isinstance(payload, dict):
        raise RuntimeError("AI 面具 JSON 行不是对象")
    mask_name = _required_json_text(payload, ("mask", "mask_name"), "mask")
    audience_archetype = _required_json_text(payload, ("aud", "audience_archetype"), "aud")
    identity_frame = _required_json_text(payload, ("frame", "identity_frame"), "frame")
    preference_tags = _required_json_tags(payload)
    return {
        "account_id": payload.get("id"),
        "mask_name": mask_name,
        "audience_archetype": audience_archetype,
        "identity_frame": identity_frame,
        "preference_tags": preference_tags,
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


def _required_json_text(payload: dict[str, Any], keys: tuple[str, str], label: str) -> str:
    for key in keys:
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    raise RuntimeError(f"AI 面具 JSON 行缺少字段: {label}")


def _required_json_tags(payload: dict[str, Any]) -> list[str]:
    value = payload.get("tags") if payload.get("tags") is not None else payload.get("preference_tags")
    tags = _string_list(value)
    if 2 <= len(tags) <= 4:
        return tags
    raise RuntimeError("AI 面具 JSON 行字段 tags 需要 2-4 个偏好标签")


def _profile_from_pipe_line(line: str) -> dict[str, Any]:
    parts = [part.strip() for part in line.split("|")]
    if len(parts) != 11:
        raise RuntimeError("AI 面具输出行字段数量错误")
    return {
        "account_id": parts[0],
        "mask_name": "",
        "audience_archetype": "",
        "identity_frame": "",
        "preference_tags": [],
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


def _normalize_generated_profile(item: Any, *, strict_lightweight: bool = False) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise RuntimeError("AI 面具输出项不是对象")
    result = dict(item)
    for key in ("persona_experiences", "consumption_experiences", "interaction_habits", "lexical_preferences", "forbidden_expressions", "preference_tags"):
        result[key] = _string_list(result.get(key))
    for key in ("mask_name", "audience_archetype", "identity_frame"):
        result[key] = str(result.get(key) or "").strip()
    _validate_generated_profile(result, int(result.get("account_id") or 0))
    if strict_lightweight:
        _validate_lightweight_summary(result, int(result.get("account_id") or 0))
        _validate_lightweight_mask_wording(result, int(result.get("account_id") or 0))
    return result


def _validate_lightweight_summary(profile: dict[str, Any], account_id: int) -> None:
    length = len(str(profile.get("short_prompt_summary") or "").strip())
    if MIN_LIGHTWEIGHT_SUMMARY_LENGTH <= length <= MAX_LIGHTWEIGHT_SUMMARY_LENGTH:
        return
    raise ValueError(
        f"voice profile summary requires {MIN_LIGHTWEIGHT_SUMMARY_LENGTH}-{MAX_LIGHTWEIGHT_SUMMARY_LENGTH} characters for account {account_id}"
    )


def _validate_lightweight_mask_wording(profile: dict[str, Any], account_id: int) -> None:
    fields = (*STRUCTURED_MASK_FIELDS, "short_prompt_summary")
    wording = " ".join(str(profile.get(field) or "").strip() for field in fields)
    if not any(term in wording for term in RESTRICTED_LIGHTWEIGHT_MASK_TERMS):
        return
    raise ValueError(f"lightweight account mask contains restricted wording for account {account_id}")


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


__all__ = [
    "VOICE_PROFILE_INITIAL_MAX_TOKENS",
    "_generate_voice_profile_payloads",
    "_parse_voice_profile_payloads",
    "_valid_summary",
    "_validate_generated_profile",
    "_validate_summary",
    "_voice_profile_ai_provider",
    "generate_lightweight_voice_profile_payloads",
]
