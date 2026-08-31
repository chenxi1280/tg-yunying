from __future__ import annotations

TRANSIENT_REVIEW_CODES = frozenset({"topic_lazy_fetch_unproven"})


def allowed_manual_review_decisions(error_code: str | None) -> tuple[str, ...]:
    decisions = ["drop", "keep_blocked"]
    if error_code in TRANSIENT_REVIEW_CODES:
        decisions.insert(0, "release")
    return tuple(decisions)


__all__ = ["allowed_manual_review_decisions"]
