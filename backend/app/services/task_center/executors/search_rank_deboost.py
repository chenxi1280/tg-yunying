from __future__ import annotations

from .search_rank_deboost_planner import build_plan
from . import search_rank_deboost_runtime as _runtime


NAVIGABLE_BUTTON_EFFECTS = _runtime.NAVIGABLE_BUTTON_EFFECTS
JOIN_CANDIDATE_EFFECTS = _runtime.JOIN_CANDIDATE_EFFECTS


def execute_search_rank_deboost(*args, **kwargs):
    _runtime.NAVIGABLE_BUTTON_EFFECTS = NAVIGABLE_BUTTON_EFFECTS
    _runtime.JOIN_CANDIDATE_EFFECTS = JOIN_CANDIDATE_EFFECTS
    return _runtime.execute_search_rank_deboost(*args, **kwargs)


__all__ = [
    "JOIN_CANDIDATE_EFFECTS",
    "NAVIGABLE_BUTTON_EFFECTS",
    "build_plan",
    "execute_search_rank_deboost",
]
