"""Preserve immutable emergency audit fields across execution-result updates."""

EMERGENCY_AUDIT_FIELDS = ("emergency_selection_id", "content_hash")


def merge_memory_execution_result(previous: dict, result: dict) -> dict:
    if not previous.get("emergency_selection_id"):
        return dict(result)
    frozen = {key: previous[key] for key in EMERGENCY_AUDIT_FIELDS if key in previous}
    if any(key in result and result[key] != value for key, value in frozen.items()):
        raise ValueError("emergency_message_memory_evidence_changed")
    return {**result, **frozen}
