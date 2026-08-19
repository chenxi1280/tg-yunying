from __future__ import annotations

from dataclasses import dataclass

from .ai_generator import AiGenerationUnavailable, ProviderRouteDeferred


@dataclass(frozen=True)
class GenerationOutcome:
    failure: AiGenerationUnavailable | None = None
    admission_deferred: bool = False
    provider_deferred: ProviderRouteDeferred | None = None


@dataclass(frozen=True)
class SequentialClaim:
    action_id: str
    owner: str
    token: str
    claimed_count: int


__all__ = ["GenerationOutcome", "SequentialClaim"]
