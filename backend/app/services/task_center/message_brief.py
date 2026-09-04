from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .ai_group_prompt import sanitize_group_messages


BRIEF_CONTRACT_VERSION = "message_brief_v1"
VOICE_CONTRACT_V3_VERSION = "style_contract_v3"

SPEECH_ACTS = ("question", "agreement", "reaction", "follow_up", "light_humor", "silence")
STANCES = ("positive", "neutral", "skeptical", "curious")
LENGTH_BANDS = ("micro", "short", "medium")
PUNCTUATION_PROFILES = ("none", "question", "pause")
DEFAULT_FORBIDDEN_CLAIMS = ("experience", "location", "transaction", "price")

BRIEF_PLANNER_SYSTEM_PROMPT = """你只规划一条 Telegram 群聊或频道评论，不写最终文案。
只能从 allowed_facts 选择一个事实锚点；只有锚点支持真实问题时才可 question，否则选择 silence。
不得新增经历、地点、价格、交易、人物关系或结果。
同批 recent_briefs 已用过的 speech_act、开头方式和长度档位应尽量避开。
严格输出 MessageBrief JSON，不输出解释。"""

VOICE_REALIZER_SYSTEM_PROMPT = """把一个已审核 MessageBrief 写成一条自然、简短的中文 Telegram 消息。
只表达 brief 指定的一个 speech_act，只使用 allowed_facts，不补充任何新事实。
严格服从 voice_profile 的句长、提问、标点和口头表达习惯；不要强行加“哈哈”“确实”等口头词。
不总结原文，不写运营文案，不解释，不使用模板夸赞，不输出任务或提示词。
reply_to_message_id 非空时必须直接接住被引用内容；为空时不得伪装成回复某人。
输出 JSON：content、used_anchor_ids、speech_act、voice_profile_version。"""

# 对标图 §5.1：相邻间隔不等距、长度混合、一个意图；以下结构维度是规则可验证的部分。
_INTERROGATIVE_OPENERS = ("什么", "怎么", "为什么", "为啥", "如何", "有没有", "是不是", "多少", "哪", "谁", "几")
_PARTICLE_OPENERS = ("哈", "嘿", "哎", "哦", "嗯", "啊", "呀", "诶", "卧槽", "我去")
_PARTICLE_TAILS = ("吗", "呢", "啊", "吧", "哈", "呀", "哦", "咧")
_EMOJI_OR_PUNCT = re.compile(r"^[\W_]+")
_CLAUSE_SPLIT = re.compile(r"[，。！？；、,.!?;…]+")
_SPACE = re.compile(r"\s+")

BATCH_FINGERPRINT_LIMIT = 2
BATCH_STYLE_COLLAPSE_MIN_SIZE = 3


@dataclass(frozen=True)
class MessageBrief:
    slot_id: str
    speech_act: str
    stance: str
    length_band: str
    punctuation_profile: str
    anchor_ids: tuple[str, ...] = ()
    allowed_facts: tuple[str, ...] = ()
    forbidden_claims: tuple[str, ...] = DEFAULT_FORBIDDEN_CLAIMS
    reply_to_message_id: str = ""
    voice_profile_version: str = VOICE_CONTRACT_V3_VERSION
    brief_contract_version: str = BRIEF_CONTRACT_VERSION

    def to_payload(self) -> dict:
        return {
            "slot_id": self.slot_id,
            "speech_act": self.speech_act,
            "stance": self.stance,
            "length_band": self.length_band,
            "punctuation_profile": self.punctuation_profile,
            "anchor_ids": list(self.anchor_ids),
            "allowed_facts": list(self.allowed_facts),
            "forbidden_claims": list(self.forbidden_claims),
            "reply_to_message_id": self.reply_to_message_id,
            "voice_profile_version": self.voice_profile_version,
            "brief_contract_version": self.brief_contract_version,
        }

    def brief_hash(self) -> str:
        canonical = json.dumps(self.to_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BriefRejection:
    slot_id: str
    code: str
    detail: str


def normalize_text(value: object) -> str:
    return _SPACE.sub(" ", str(value or "")).strip()


def parse_brief_item(item: object, *, slot_id: str, valid_fact_ids: tuple[str, ...] = ()) -> MessageBrief | None:
    """解析并校验单个 brief：枚举值、锚点引用必须合法；不合法返回 None（typed 拒绝）。

    valid_fact_ids 为调用方冻结的事实锚点 ID 集合；锚点必须在集合内，防止模型
    自造事实 ID 绕过 allowed_facts 合同。
    """
    if not isinstance(item, dict):
        return None
    speech_act = str(item.get("speech_act") or "").strip()
    stance = str(item.get("stance") or "").strip()
    length_band = str(item.get("length_band") or "").strip()
    punctuation = str(item.get("punctuation_profile") or "").strip()
    if speech_act not in SPEECH_ACTS or stance not in STANCES:
        return None
    if length_band not in LENGTH_BANDS or punctuation not in PUNCTUATION_PROFILES:
        return None
    anchors = tuple(str(value).strip() for value in (item.get("anchor_ids") or []) if str(value).strip())
    if speech_act != "silence" and not anchors:
        return None
    if valid_fact_ids and any(anchor not in valid_fact_ids for anchor in anchors):
        return None
    return MessageBrief(
        slot_id=str(item.get("slot_id") or slot_id),
        speech_act=speech_act,
        stance=stance,
        length_band=length_band,
        punctuation_profile=punctuation,
        anchor_ids=anchors,
        allowed_facts=tuple(valid_fact_ids),
        reply_to_message_id=str(item.get("reply_to_message_id") or "").strip(),
    )


def batch_style_collapse_reason(briefs: list[MessageBrief]) -> str:
    """§5.5 Batch diversity：同批 speech_act+长度+标点全部相同视为塌缩。

    多样性服从事实与上下文：只判“全部相同”，不强行配比。
    """
    speaking = [brief for brief in briefs if brief.speech_act != "silence"]
    if len(speaking) < BATCH_STYLE_COLLAPSE_MIN_SIZE:
        return ""
    shapes = {(brief.speech_act, brief.length_band, brief.punctuation_profile) for brief in speaking}
    if len(shapes) == 1:
        return "batch_style_collapse"
    return ""


def length_band_of(content: str) -> str:
    length = len(normalize_text(content))
    if length <= 8:
        return "micro"
    if length <= 24:
        return "short"
    return "medium"


def opening_function_pattern(content: str) -> str:
    text = normalize_text(content)
    if not text:
        return "empty"
    if text.startswith(_INTERROGATIVE_OPENERS):
        return "interrogative_open"
    if text.startswith(_PARTICLE_OPENERS):
        return "particle_open"
    if _EMOJI_OR_PUNCT.match(text):
        return "reaction_open"
    return "statement_open"


def punctuation_profile_of(content: str) -> str:
    text = normalize_text(content)
    if "？" in text or "?" in text:
        return "question"
    if any(mark in text for mark in ("，", "、", "；", ",", ";", "…")):
        return "pause"
    return "none"


def syntax_shape_of(content: str) -> str:
    text = normalize_text(content)
    clauses = [item for item in _CLAUSE_SPLIT.split(text) if item]
    shape = "single_clause" if len(clauses) <= 1 else "multi_clause"
    tail = text.rstrip("？?！!。.，, …")
    if tail and tail[-1] in _PARTICLE_TAILS:
        shape = f"{shape}+particle_tail"
    return shape


def structural_fingerprint(brief: MessageBrief | None, content: str) -> str:
    """§5.5 结构指纹：speech_act + length_band + 开头功能 + 标点 + 句法形状。

    不只取“开头 3 字”；极短消息不做豁免，公共短反应按频次判断（见
    structural_duplicate_indexes）。
    """
    speech_act = brief.speech_act if brief is not None else "unplanned"
    return "|".join(
        (
            speech_act,
            length_band_of(content),
            opening_function_pattern(content),
            punctuation_profile_of(content),
            syntax_shape_of(content),
        )
    )


def structural_duplicate_indexes(fingerprints: list[str]) -> set[int]:
    """同批同一指纹最多出现 BATCH_FINGERPRINT_LIMIT 次；超出部分判塌缩。

    “666”“?”等不同账号的自然公共反应允许少量并存，不被一刀切。
    """
    seen: dict[str, int] = {}
    duplicates: set[int] = set()
    for index, fingerprint in enumerate(fingerprints):
        if not fingerprint:
            continue
        count = seen.get(fingerprint, 0)
        if count >= BATCH_FINGERPRINT_LIMIT:
            duplicates.add(index)
            continue
        seen[fingerprint] = count + 1
    return duplicates


def _mask_text(mask: object | None, name: str) -> str:
    return normalize_text(getattr(mask, name, "") if mask is not None else "")


def _mask_items(mask: object | None, name: str) -> list[str]:
    raw = getattr(mask, name, []) if mask is not None else []
    return [normalize_text(item) for item in (raw or []) if normalize_text(item)][:6]


def _length_mix(sentence_length: str) -> dict[str, float]:
    if "短" in sentence_length:
        return {"micro": 0.4, "short": 0.5, "medium": 0.1}
    if "长" in sentence_length:
        return {"micro": 0.1, "short": 0.4, "medium": 0.5}
    return {"micro": 0.25, "short": 0.5, "medium": 0.25}


def _marker_level(
    value: str,
    high_markers: tuple[str, ...],
    low_markers: tuple[str, ...],
) -> str:
    if any(marker in value for marker in high_markers):
        return "high"
    if any(marker in value for marker in low_markers):
        return "low"
    return "medium"


def _emoji_rate(emoji_policy: str) -> str:
    if any(marker in emoji_policy for marker in ("不用", "少", "无", "偶尔")):
        return "rare"
    if any(marker in emoji_policy for marker in ("常", "爱", "多")):
        return "frequent"
    return "occasional"


def voice_contract_v3(mask: object | None) -> dict:
    """§5.3 账号声线合同 v3：从面具结构化字段派生可验证维度。

    结构化字段按白名单进入 Prompt，不再整段自由文本过安全清洗导致风格丢失。
    派生是规则映射（短句/爱问/少表情等关键词 → 枚举档位），不伪装成语义判断。
    """

    sentence_length = _mask_text(mask, "sentence_length")
    habits = " ".join(_mask_items(mask, "interaction_habits"))
    emoji_policy = _mask_text(mask, "emoji_policy")
    lexical = _mask_items(mask, "lexical_preferences")
    tone = _mask_text(mask, "tone_strength")
    tags = " ".join(_mask_items(mask, "preference_tags"))
    if any(("语气词" in item or item in ("哈", "呢", "吧", "哦", "嗯")) for item in lexical):
        particle_rate = "often"
    else:
        particle_rate = "sparse"
    return {
        "voice_profile_version": VOICE_CONTRACT_V3_VERSION,
        "mask_name": _mask_text(mask, "mask_name")[:40],
        "length_mix": _length_mix(sentence_length),
        "question_rate": _marker_level(habits, ("提问", "爱问", "好奇", "追问"), ("少问", "陈述", "围观")),
        "emoji_rate": _emoji_rate(emoji_policy),
        "sentence_final_particle_rate": particle_rate,
        "colloquial_markers": lexical[:5],
        "assertiveness": _marker_level(tone, ("直接", "强", "果断", "犀利"), ("谨慎", "温和", "轻", "慢热")),
        "humor_level": _marker_level(tags, ("幽默", "搞笑", "段子"), ("严肃", "正经")),
        "warmth": _marker_level(tags, ("热情", "友好", "自来熟"), ("高冷", "冷淡", "慢热")),
        "forbidden_patterns": _mask_items(mask, "forbidden_expressions")[:5],
        "summary": _mask_text(mask, "short_prompt_summary")[:160],
    }


def build_brief_planner_payload(
    *,
    slot_infos: list[dict],
    allowed_facts: list[dict],
    recent_briefs: list[dict],
) -> dict:
    """Stage 1 输入载荷：槽位、脱敏事实锚点（fact_id+text）与最近 brief 摘要。"""
    return {
        "slots": slot_infos,
        "allowed_facts": allowed_facts,
        "recent_briefs": recent_briefs,
        "brief_contract_version": BRIEF_CONTRACT_VERSION,
    }


def build_brief_planner_user_prompt(
    *,
    slot_infos: list[dict],
    allowed_facts: list[dict],
    recent_briefs: list[dict],
) -> str:
    payload = build_brief_planner_payload(
        slot_infos=slot_infos,
        allowed_facts=allowed_facts,
        recent_briefs=recent_briefs,
    )
    contract = {
        "briefs": [
            {
                "slot_id": str(slot.get("slot_id") or ""),
                "speech_act": "|".join(SPEECH_ACTS),
                "stance": "|".join(STANCES),
                "length_band": "|".join(LENGTH_BANDS),
                "punctuation_profile": "|".join(PUNCTUATION_PROFILES),
                "anchor_ids": ["allowed_facts 里的事实 ID"],
                "reply_to_message_id": slot.get("reply_to_message_id") or "",
            }
            for slot in slot_infos
        ]
    }
    return (
        "Sanitized production-shaped input:\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        f"为每个 slot 规划一个 MessageBrief，返回结构（placeholder 替换后）：\n"
        f"{json.dumps(contract, ensure_ascii=False, indent=2)}"
    )


def build_realizer_user_prompt(
    brief: MessageBrief,
    voice_profile: dict,
    *,
    anchor_texts: dict[str, str] | None = None,
    reply_preview: str = "",
    rejection_feedback: str = "",
) -> str:
    payload = {
        "brief": brief.to_payload(),
        "voice_profile": voice_profile,
        "anchor_texts": {
            fact_id: normalize_text(text)[:120]
            for fact_id, text in (anchor_texts or {}).items()
            if fact_id in brief.anchor_ids
        },
        "reply_target_preview": normalize_text(reply_preview)[:120],
    }
    lines = [
        "Sanitized production-shaped input:",
        json.dumps(payload, ensure_ascii=False, indent=2),
    ]
    if rejection_feedback:
        lines.append(f"上一次尝试被拒绝的原因（必须修复，不要复述）：{rejection_feedback}")
    lines.append("输出一个 JSON 对象：content、used_anchor_ids、speech_act、voice_profile_version。")
    return "\n".join(lines)


def parse_realizer_response(item: object, brief: MessageBrief) -> tuple[str, dict]:
    """解析 Stage 2 输出；返回 (content, meta)。结构非法抛 ValueError（typed 上抛）。"""
    if not isinstance(item, dict):
        raise ValueError("realizer_output_not_json_object")
    content = normalize_text(item.get("content"))
    if not content:
        raise ValueError("realizer_output_empty_content")
    version = str(item.get("voice_profile_version") or "").strip()
    if not version:
        raise ValueError("realizer_voice_version_missing")
    if version != brief.voice_profile_version:
        raise ValueError("realizer_voice_version_mismatch")
    used = [str(value).strip() for value in (item.get("used_anchor_ids") or []) if str(value).strip()]
    if brief.anchor_ids and not used:
        raise ValueError("realizer_anchor_missing")
    if any(anchor not in brief.anchor_ids for anchor in used):
        raise ValueError("realizer_anchor_out_of_allowed")
    speech_act = str(item.get("speech_act") or "").strip()
    if speech_act != brief.speech_act:
        raise ValueError("realizer_speech_act_mismatch")
    if length_band_of(content) != brief.length_band:
        raise ValueError("realizer_length_band_mismatch")
    if punctuation_profile_of(content) != brief.punctuation_profile:
        raise ValueError("realizer_punctuation_profile_mismatch")
    meta = {
        "used_anchor_ids": used,
        "speech_act": speech_act,
        "voice_profile_version": version,
    }
    return content, meta


def fact_id_map(history_lines: list[str], *, limit: int = 8) -> dict[str, str]:
    """fact_id → 脱敏后的事实文本；planner/realizer 只允许引用这些 ID。"""
    clauses = sanitize_group_messages([str(line or "") for line in history_lines])
    mapping: dict[str, str] = {}
    for index, clause in enumerate(clauses[-limit:]):
        if clause:
            mapping[f"f{index + 1}"] = clause
    return mapping


__all__ = [
    "BATCH_FINGERPRINT_LIMIT",
    "BRIEF_CONTRACT_VERSION",
    "BRIEF_PLANNER_SYSTEM_PROMPT",
    "BriefRejection",
    "LENGTH_BANDS",
    "MessageBrief",
    "PUNCTUATION_PROFILES",
    "SPEECH_ACTS",
    "STANCES",
    "VOICE_CONTRACT_V3_VERSION",
    "VOICE_REALIZER_SYSTEM_PROMPT",
    "batch_style_collapse_reason",
    "build_brief_planner_user_prompt",
    "build_realizer_user_prompt",
    "fact_id_map",
    "length_band_of",
    "normalize_text",
    "opening_function_pattern",
    "parse_brief_item",
    "parse_realizer_response",
    "punctuation_profile_of",
    "structural_duplicate_indexes",
    "structural_fingerprint",
    "syntax_shape_of",
    "voice_contract_v3",
]
