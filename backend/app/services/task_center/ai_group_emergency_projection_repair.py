"""Align the exact pre-Gateway emergency revision left by the first release."""
from sqlalchemy import select, update

from app.models import Action, FulfillmentObligationProjection, Task

from .ai_group_emergency_contract import telegram_unattempted
from .payloads import SendMessagePayload


REPAIRABLE_ACTION_STATES = frozenset({"pending", "claiming", "executing"})
REPAIR_AUDIT_KEY = "emergency_projection_repair"


def align_existing_emergency_projection(session, action) -> None:
    from .ai_group_emergency import validate_emergency_content_binding

    projection = _lock_original_owners(session, action)
    payload = SendMessagePayload.model_validate(action.payload)
    selection = validate_emergency_content_binding(session, action, payload)
    if not _same_owner(action, projection):
        raise ValueError("emergency_projection_repair_owner_changed")
    target_version = selection.materialization_version
    if projection.materialization_version == target_version:
        if action.materialization_version != target_version:
            raise ValueError("emergency_projection_repair_action_version_invalid")
        return
    if (projection.state != "open" or action.status not in REPAIRABLE_ACTION_STATES
            or projection.materialization_version + 1 != target_version
            or action.materialization_version not in {projection.materialization_version, target_version}
            or not telegram_unattempted(session, action)):
        raise ValueError("emergency_projection_repair_not_exact_uncalled_revision")
    _advance_projection(session, action, projection=projection, selection=selection)


def _lock_original_owners(session, action):
    with session.no_autoflush:
        task = session.scalar(select(Task).where(Task.id == action.task_id).with_for_update())
        locked_action = session.scalar(select(Action).where(Action.id == action.id).with_for_update())
        if task is None or locked_action is None:
            raise ValueError("emergency_projection_repair_owner_missing")
        # Preserve caller changes only after acquiring Task -> Action in that order.
        session.flush()
        session.refresh(task)
        session.refresh(action)
        return session.scalar(select(FulfillmentObligationProjection).where(
            FulfillmentObligationProjection.obligation_type == action.obligation_type,
            FulfillmentObligationProjection.obligation_id == action.obligation_id,
        ).with_for_update().execution_options(populate_existing=True))


def _same_owner(action, projection) -> bool:
    return bool(projection and (
        projection.tenant_id, projection.task_id, projection.task_lifecycle_epoch, projection.active_action_id,
    ) == (action.tenant_id, action.task_id, action.task_lifecycle_epoch, action.id))


def _advance_projection(session, action, *, projection, selection) -> None:
    previous_version = projection.version
    previous_materialization = projection.materialization_version
    changed = session.execute(update(FulfillmentObligationProjection).where(
        FulfillmentObligationProjection.id == projection.id,
        FulfillmentObligationProjection.active_action_id == action.id,
        FulfillmentObligationProjection.state == "open",
        FulfillmentObligationProjection.version == previous_version,
        FulfillmentObligationProjection.materialization_version == previous_materialization,
    ).values(materialization_version=selection.materialization_version, version=previous_version + 1)
        .execution_options(synchronize_session=False)).rowcount
    if changed != 1:
        raise ValueError("emergency_projection_repair_conflict")
    action.result = {**dict(action.result or {}), REPAIR_AUDIT_KEY: {
        "selection_id": selection.id, "previous_action_materialization_version": action.materialization_version,
        "previous_materialization_version": previous_materialization,
        "materialization_version": selection.materialization_version,
        "previous_projection_version": previous_version, "projection_version": previous_version + 1,
    }}
    action.materialization_version = selection.materialization_version
    session.refresh(projection)
    session.flush()
