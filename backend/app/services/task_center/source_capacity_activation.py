from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SourcePacingCapacityPolicyVersion


SOURCE_CAPACITY_DOMAINS = {
    "group_ai_chat": "ai_send",
    "channel_comment": "comment",
    "channel_like": "reaction",
    "channel_view": "view",
}


def validate_source_capacity_config(
    session: Session,
    tenant_id: int,
    task_type: str,
    pacing_config: dict,
) -> None:
    if not pacing_config.get("source_capacity_v2_enabled"):
        return
    domain = SOURCE_CAPACITY_DOMAINS.get(task_type)
    if domain is None:
        raise ValueError("source_capacity_task_type_invalid")
    policy_id = str(pacing_config.get("source_capacity_policy_version_id") or "")
    policy = session.scalar(select(SourcePacingCapacityPolicyVersion).where(
        SourcePacingCapacityPolicyVersion.id == policy_id,
        SourcePacingCapacityPolicyVersion.tenant_id == tenant_id,
        SourcePacingCapacityPolicyVersion.pacing_domain == domain,
        SourcePacingCapacityPolicyVersion.status == "active",
    ))
    if policy is None:
        raise ValueError("source_capacity_policy_not_active")


__all__ = ["SOURCE_CAPACITY_DOMAINS", "validate_source_capacity_config"]
