from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from .ai_generation_dependencies import GenerationDependencies
from .ai_group_prompt import LEGACY_NEGATIVE_PHRASES
from .two_stage_generation import two_stage_enabled


AI_GENERATION_DEADLINE_BUDGET_EXHAUSTED = "ai_generation_deadline_budget_exhausted"


@dataclass(frozen=True)
class SlotGenerationResult:
    content: str
    rejection_code: str = ""
    rejection_detail: str = ""
    voice_profile_anchor_rewritten: bool = False
    quality_fallback: str = ""
    fallback_reason: str = ""
    evaluator_evidence: dict = field(default_factory=dict)


@dataclass
class TwoStageRuntime:
    session: Session
    request: object
    dependencies: GenerationDependencies
    history_lines: list[str]
    baseline: list[str]
    fingerprint_counts: dict[str, int]


def naive_datetime(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value


def legacy_negative_match(request, content: str) -> bool:
    if two_stage_enabled(getattr(request, "config", {})):
        return False
    return any(phrase in content for phrase in LEGACY_NEGATIVE_PHRASES)


__all__ = [
    "AI_GENERATION_DEADLINE_BUDGET_EXHAUSTED",
    "SlotGenerationResult",
    "TwoStageRuntime",
    "legacy_negative_match",
    "naive_datetime",
]
