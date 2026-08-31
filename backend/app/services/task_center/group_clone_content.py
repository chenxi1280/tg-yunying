from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.models import RuleSetVersion, TgGroup
from app.services.content_filters import filter_outbound_content
from app.services.rule_engine import apply_output_policy, evaluate_input_filter


class CloneContentReviewRequired(RuntimeError):
    pass


def sanitize_clone_content(session, task, *, config, event):
    version = session.scalar(select(RuleSetVersion).where(
        RuleSetVersion.tenant_id == task.tenant_id,
        RuleSetVersion.rule_set_id == config.content.rule_set_id,
        RuleSetVersion.version == config.content.rule_set_version,
        RuleSetVersion.status == "published",
    ))
    if version is None:
        raise RuntimeError("group_clone_frozen_rule_version_missing")
    admitted = evaluate_input_filter(
        event.content, event.sender_peer_id or "",
        event.media_type or "text", version.filters,
    )
    if not admitted.passed:
        return None
    output = apply_output_policy(
        event.content, version.output_checks, version.transforms,
    )
    if not output.allowed:
        return None
    if output.content != event.content and event.entities:
        raise CloneContentReviewRequired(
            "group_clone_entity_rebuild_required_after_transform"
        )
    target_group = session.get(TgGroup, config.target.internal_group_id)
    filtered = filter_outbound_content(
        session, tenant_id=task.tenant_id,
        group=target_group, content=output.content,
    )
    if not filtered.ok:
        return None
    if filtered.content != output.content and event.entities:
        raise CloneContentReviewRequired(
            "group_clone_entity_rebuild_required_after_sanitization"
        )
    return filtered.content


def sanitize_clone_edit(session, task, *, config, event, obligation):
    if event.protected_content:
        obligation.state = "waiting_manual_review"
        obligation.error_code = "protected_content"
        return None
    try:
        content = sanitize_clone_content(
            session, task, config=config, event=event,
        )
    except CloneContentReviewRequired as exc:
        obligation.state = "waiting_manual_review"
        obligation.error_code = str(exc)
        return None
    if content is None:
        obligation.state = "filtered"
        obligation.resolved_at = datetime.now(timezone.utc)
    return content


def clone_content_entities(event, content: str) -> list[dict]:
    entities = [dict(item) for item in (event.entities or ())]
    if not entities or content == event.content:
        return entities
    if content.endswith(event.content):
        prefix = content[:-len(event.content)]
        utf16_shift = len(prefix.encode("utf-16-le")) // 2
        return [{**item, "offset": int(item["offset"]) + utf16_shift} for item in entities]
    raise CloneContentReviewRequired(
        "group_clone_entity_rebuild_required_after_sanitization"
    )


__all__ = [
    "CloneContentReviewRequired",
    "clone_content_entities",
    "sanitize_clone_content",
    "sanitize_clone_edit",
]
