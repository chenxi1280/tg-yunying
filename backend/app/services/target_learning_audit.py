from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Task
from app.services._common import audit


def audit_learning_profile_use(session: Session, task: Task, profile_preview: dict, actor: str) -> None:
    profile_id = str(profile_preview.get("profile_id") or "")
    if not profile_id or not int(profile_preview.get("profile_version") or 0):
        return
    audit(
        session,
        tenant_id=task.tenant_id,
        actor=actor,
        action="AI使用目标画像",
        target_type="target_learning_profile",
        target_id=profile_id,
        detail=f"task_id={task.id}; scene={profile_preview.get('profile_scene')}; version={profile_preview.get('profile_version')}",
    )
