from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import AiContentPolicyVersion, Task

from .ai_content_policy import (
    AiContentPolicyConflict,
    TaskBindingSpec,
    bind_task_policy,
    validate_task_policy_binding,
)
from .ai_provider_routes import (
    COMMENT_REALIZE_PURPOSE,
    COMMENT_REVIEW_PURPOSE,
    COMMENT_ROUTE_PURPOSE,
    GROUP_REVIEW_PURPOSE,
    GROUP_ROUTE_PURPOSE,
    REALIZE_PURPOSE_BY_MODE,
    active_route_snapshot,
    ProviderRouteUnavailable,
)


def activate_task_ai_content_config(session: Session, task: Task) -> None:
    spec = validate_task_ai_content_config(session, task)
    if spec is not None:
        bind_task_policy(session, spec)


def validate_task_ai_content_config(
    session: Session,
    task: Task,
) -> TaskBindingSpec | None:
    config = dict(task.type_config or {})
    if not config.get("ai_content_route_v2_enabled"):
        return None
    if not config.get("ai_two_stage_enabled"):
        raise ValueError("ai_content_route_v2_requires_two_stage")
    policy_id = str(config.get("ai_content_policy_version_id") or "")
    policy = session.get(AiContentPolicyVersion, policy_id)
    if policy is None or policy.status != "active" or policy.tenant_id != task.tenant_id:
        raise ValueError("ai_content_binding_policy_not_active")
    routes = tuple(str(item) for item in config.get("ai_content_allowed_routes") or ())
    try:
        _validate_provider_routes(session, task, routes)
        spec = TaskBindingSpec(
            task_id=task.id,
            policy_version_id=policy_id,
            allowed_routes=routes,
            attestation_ids=tuple(config.get("ai_content_attestation_ids") or ()),
            scope_refs=_scope_refs(task, config),
            approved_by=policy.approved_by,
        )
        validate_task_policy_binding(session, spec)
        return spec
    except (AiContentPolicyConflict, ProviderRouteUnavailable) as exc:
        raise ValueError(str(exc)) from exc


def _validate_provider_routes(
    session: Session,
    task: Task,
    routes: tuple[str, ...],
) -> None:
    purposes = _provider_purposes(task.type, routes)
    for purpose in purposes:
        active_route_snapshot(session, task.tenant_id, purpose)


def _provider_purposes(task_type: str, routes: tuple[str, ...]) -> tuple[str, ...]:
    if task_type == "channel_comment":
        return (COMMENT_ROUTE_PURPOSE, COMMENT_REALIZE_PURPOSE, COMMENT_REVIEW_PURPOSE)
    if task_type != "group_ai_chat":
        raise ValueError("ai_content_route_v2_task_type_invalid")
    realize = tuple(REALIZE_PURPOSE_BY_MODE[route] for route in routes)
    return (GROUP_ROUTE_PURPOSE, *realize, GROUP_REVIEW_PURPOSE)


def _scope_refs(task: Task, config: dict) -> tuple[tuple[str, str], ...]:
    if not any(route != "general" for route in config.get("ai_content_allowed_routes") or ()):
        return ()
    if task.type == "group_ai_chat":
        scope_id = str(config.get("target_group_id") or "")
        scope_type = "task_group"
    else:
        scope_id = str(config.get("target_channel_id") or "")
        scope_type = "task_source"
    if not scope_id:
        raise ValueError("adult_attestation_scope_missing")
    return ((scope_type, scope_id),)


__all__ = ["activate_task_ai_content_config", "validate_task_ai_content_config"]
