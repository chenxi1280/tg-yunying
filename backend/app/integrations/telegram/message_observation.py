"""Content-free diagnostics for one bounded Telegram history read."""
from dataclasses import dataclass

from .contracts import GroupMessageSnapshot


@dataclass(frozen=True)
class GroupMessageObservation:
    messages: tuple[GroupMessageSnapshot, ...]
    diagnostics: dict


def message_observation(raw, candidates, snapshots, *, limit, after_message_id, control_only):
    dates = sorted(message.date for message in raw if getattr(message, "date", None))
    return GroupMessageObservation(tuple(snapshots), {
        "read_status": "observed",
        "raw_message_count": len(raw),
        "candidate_message_count": len(candidates),
        "excluded_no_controls_count": len(raw) - len(candidates) if control_only else 0,
        "snapshot_skipped_count": len(candidates) - len(snapshots),
        "after_message_id": after_message_id,
        "limit": limit,
        "limit_reached": len(raw) >= limit,
        "first_message_at": dates[0].isoformat() if dates else None,
        "last_message_at": dates[-1].isoformat() if dates else None,
    })
