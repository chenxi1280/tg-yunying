from __future__ import annotations

import hashlib


def derive_deterministic_random_id(
    contract: str,
    tenant_id: int,
    *,
    task_id: str,
    epoch: int,
    obligation_id: str,
    mutation_kind: str,
    part_index: int,
    derivation_version: int = 1,
    collision_nonce: int = 0,
) -> int:
    raw = (
        f"{contract}:{tenant_id}:{task_id}:{epoch}:{obligation_id}:"
        f"{mutation_kind}:{part_index}:{derivation_version}:{collision_nonce}"
    )
    value = int.from_bytes(hashlib.sha256(raw.encode("utf-8")).digest()[:8], "big", signed=True)
    return value or 1


__all__ = ["derive_deterministic_random_id"]
