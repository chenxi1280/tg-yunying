"""Validate persisted service-owned fields without exposing new API settings."""
from pydantic import BaseModel, Field

from .ai_content_policy import VALID_ROUTES


class GroupInternalConfig(BaseModel):
    ai_content_context_route: str | None = None
    group_rescue_admin_account_id: int | None = Field(default=None, ge=0)


def separate_group_internal_config(task_type: str, config: dict) -> tuple[dict, dict]:
    if task_type != "group_ai_chat":
        return dict(config), {}
    fields = GroupInternalConfig.model_fields
    supplied = {key: value for key, value in config.items() if key in fields}
    internal = GroupInternalConfig.model_validate(supplied).model_dump(exclude_unset=True)
    route = internal.get("ai_content_context_route")
    if route and route not in VALID_ROUTES:
        raise ValueError("ai_content_context_route_invalid")
    return {key: value for key, value in config.items() if key not in fields}, internal
