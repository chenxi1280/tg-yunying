from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FullInitializationClaim:
    initialization_id: int
    stage: str
    lease_token: str


__all__ = ["FullInitializationClaim"]
