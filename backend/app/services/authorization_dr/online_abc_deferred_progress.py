from __future__ import annotations

import re
from collections import Counter


def start_counts(detail: str) -> Counter:
    counts: Counter = Counter()
    for name in ("succeeded", "manual_required", "deferred_reconcile"):
        counts[name] = _int_value(detail, f"start_{name}")
    return counts


def business_delta(current: Counter, start: Counter) -> dict:
    return {
        "succeeded": current["succeeded"] - start["succeeded"],
        "manual_required": current["manual_required"] - start["manual_required"],
        "deferred_reconcile": current["deferred_reconcile"] - start["deferred_reconcile"],
    }


def _int_value(detail: str, key: str) -> int:
    match = re.search(rf"(?:^|; ){re.escape(key)}=([0-9]+)(?:;|$)", detail or "")
    return int(match.group(1)) if match else 0
