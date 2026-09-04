"""Business source classification, separate from text-generation quality filters."""
import re
from datetime import timedelta

from app.timezone import as_beijing
from .source_pacing import rolling_source_window


AD_MARKERS = re.compile(r"#ad\b|#sponsor\b|广告推广|第三方推广", re.IGNORECASE)
SOURCE_EXPECTATION_MODES = frozenset({"continuous_event_driven", "finite_existing_sources", "promised_daily_sources"})


def logical_source_key(message) -> str:
    album = str(message.grouped_id or "")
    return f"album:{album}" if album else f"message:{message.message_id}"


def source_window_end(task, message):
    if task.type == "channel_view":
        active_days = int((task.type_config or {}).get("message_active_days") or 7)
        return as_beijing(message.published_at or message.created_at) + timedelta(days=active_days)
    if task.type == "channel_comment":
        return rolling_source_window(task, message.published_at or message.created_at)[1]
    return rolling_source_window(task, message.created_at)[1]


def source_filter_reason(message, *, task_type: str) -> str:
    if task_type not in {"channel_comment", "channel_like"}:
        return ""
    metadata = dict(message.source_metadata or {})
    if not metadata.get("observed"):
        return "source_metadata_unproven"
    if metadata.get("service_action"):
        return "service_action"
    if metadata.get("poll"):
        return "poll_or_quiz"
    if metadata.get("forwarded") and metadata.get("forward_peer_id") and metadata.get("forwarded_external", True) and AD_MARKERS.search(message.content_preview or ""):
        return "external_ad_forward"
    return ""


def source_opportunity_state(mode: str, *, complete: bool, has_sources: bool, day_closed: bool = False) -> str:
    if mode not in SOURCE_EXPECTATION_MODES:
        raise ValueError("source_expectation_mode_invalid")
    if not complete:
        return "source_ingestion_unproven"
    if has_sources:
        return "sources_available"
    if mode == "finite_existing_sources":
        return "missed_no_source"
    if not day_closed:
        return "waiting_no_opportunity"
    return "neutral_no_opportunity" if mode == "continuous_event_driven" else "missed_promised_source"
