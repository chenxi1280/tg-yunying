"""7-day deterministic group-visible daily theme rotation engine for AI group chats."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib


DAILY_THEME_VERSION = "daily_theme_v2"


@dataclass(frozen=True)
class DailyVocabularyTheme:
    theme_id: int
    name: str
    description: str
    tone_guidance: str
    prohibited_items: str


THEME_PALETTES: tuple[DailyVocabularyTheme, ...] = (
    DailyVocabularyTheme(
        theme_id=0,
        name="短促随性",
        description="手机单手打字，极简短句，随性老哥",
        tone_guidance="对当前 act type 优先选择 8～14 汉字的短连接词、语气词和收尾词，短促利落，空格断句。",
        prohibited_items="不使用 2 字附和或 +1 绕过 8~20 汉字长度门；不生造长难句。",
    ),
    DailyVocabularyTheme(
        theme_id=1,
        name="陈述质感",
        description="注重真实体验反馈，客观陈述与评价",
        tone_guidance="当 act type 本来就是 statement/evaluation 时提高评价、感叹类词权重；其他 act type 使用中性变体。",
        prohibited_items="不把 question/reply 改成评价，紧扣群聊语境自然表达。",
    ),
    DailyVocabularyTheme(
        theme_id=2,
        name="求证质感",
        description="注重细节核实与真实打听，谨慎求证",
        tone_guidance="当 act type 本来就是 question/detail_follow 时提高具体求证词权重；其他 act type 使用谨慎措辞。",
        prohibited_items="不制造新问题，不得泛问“大家怎么看”，无上下文时只提开放问题。",
    ),
    DailyVocabularyTheme(
        theme_id=3,
        name="承接质感",
        description="紧扣群聊上下文顺势接话，自然转折",
        tone_guidance="对 reply/supplement 使用转折、承接和收尾表达，自然搭话。",
        prohibited_items="不把 direct 改成 reply，不新增未经证明的事实。",
    ),
    DailyVocabularyTheme(
        theme_id=4,
        name="保留质感",
        description="防踩雷与避坑提醒，持审慎保留态度",
        tone_guidance="当 stance 已冻结为 disagreement/reserved 时使用克制分歧词；其他 stance 使用中性保留词。",
        prohibited_items="不改变 stance，不攻击群友，不编造踩雷事实。",
    ),
    DailyVocabularyTheme(
        theme_id=5,
        name="轻松质感",
        description="随性调侃，氛围轻松，偶带 1 个常用 emoji",
        tone_guidance="在已允许的 mood/persona 内提高轻松语气词权重，可选最多一个真实 emoji（😂/👍/🔥/👀）。",
        prohibited_items="不改变 mood，不用调侃掩盖事实问题或违反安全红线。",
    ),
    DailyVocabularyTheme(
        theme_id=6,
        name="均衡质感",
        description="多维度视角均衡，低频词汇自然表达",
        tone_guidance="在当前 allocation plan 已冻结的 act-type mix 内均衡选择低频表面变体，表达自然。",
        prohibited_items="不重新分配 act type，不让多个账号生成同构句式。",
    ),
)


@dataclass(frozen=True)
class DailyExpressionContext:
    surface_scope_key: str
    task_day: date
    allocation_plan_id: str
    plan_unit_ordinal: int
    relation_kind: str
    act_type: str
    stance: str
    topic_mode: str
    vocabulary_theme_id: int
    vocabulary_sample_ids: tuple[str, ...] = ()
    vocabulary_surface_terms: tuple[str, ...] = ()
    vocabulary_normalized_term_ids: tuple[str, ...] = ()
    vocabulary_effective_state: str = "not_applicable"


def get_daily_theme_index(surface_scope_key: str, task_day: date) -> int:
    seed = ("ai-group-theme-v2:" + str(surface_scope_key).strip()).encode("utf-8")
    digest = hashlib.sha256(seed).digest()
    surface_offset = int.from_bytes(digest[:8], "big") % 7
    return (task_day.toordinal() + surface_offset) % 7


def get_daily_vocabulary_theme(
    surface_scope_key: str,
    task_day: date,
) -> DailyVocabularyTheme:
    idx = get_daily_theme_index(surface_scope_key, task_day)
    return THEME_PALETTES[idx]


def get_vocabulary_theme(theme_id: int) -> DailyVocabularyTheme:
    if theme_id < 0 or theme_id >= len(THEME_PALETTES):
        raise ValueError("daily_theme_contract_invalid")
    return THEME_PALETTES[theme_id]


__all__ = [
    "DailyExpressionContext",
    "DailyVocabularyTheme",
    "DAILY_THEME_VERSION",
    "THEME_PALETTES",
    "get_daily_theme_index",
    "get_daily_vocabulary_theme",
    "get_vocabulary_theme",
]
