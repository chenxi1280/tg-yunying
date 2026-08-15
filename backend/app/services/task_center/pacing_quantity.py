from __future__ import annotations

import hashlib
import json
import math


def deterministic_quantity_with_jitter(
    target: int,
    jitter: float,
    *,
    seed_id: str,
) -> int:
    base = max(0, int(target or 0))
    ratio = min(1.0, max(0.0, float(jitter or 0)))
    if base == 0 or ratio == 0:
        return base
    lower = max(0, math.floor(base * (1 - ratio)))
    upper = max(lower, math.ceil(base * (1 + ratio)))
    canonical = json.dumps([seed_id, base, ratio], separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).digest()
    return lower + int.from_bytes(digest[:8], "big") % (upper - lower + 1)


def deterministic_rank(seed_id: str, identity: str) -> str:
    canonical = json.dumps([seed_id, identity], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = ["deterministic_quantity_with_jitter", "deterministic_rank"]
