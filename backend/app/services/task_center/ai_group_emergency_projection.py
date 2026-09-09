"""Keep emergency content revisions aligned with the original obligation owner."""
from sqlalchemy import select

from app.models import FulfillmentObligationProjection


def lock_emergency_projection(session, action):
    from .fulfillment_remote_facts import ensure_action_obligation

    if not ensure_action_obligation(session, action):
        return None
    projection = _projection(session, action, lock=True)
    if (projection is None or projection.state != "open"
            or not _same_owner(action, projection)
            or projection.materialization_version != action.materialization_version):
        raise ValueError("emergency_obligation_projection_invalid")
    return projection


def advance_emergency_projection(action, projection, *, selection) -> None:
    if not _same_owner(action, projection) or projection.materialization_version + 1 != selection.materialization_version:
        raise ValueError("emergency_obligation_projection_changed")
    projection.materialization_version = selection.materialization_version
    projection.version = int(projection.version or 1) + 1


def validate_emergency_projection(session, action, *, selection) -> None:
    projection = _projection(session, action, lock=False)
    if (projection is None or not _same_owner(action, projection)
            or projection.materialization_version != selection.materialization_version):
        raise ValueError("emergency_obligation_projection_binding_invalid")


def _projection(session, action, *, lock):
    statement = select(FulfillmentObligationProjection).where(
        FulfillmentObligationProjection.obligation_type == action.obligation_type,
        FulfillmentObligationProjection.obligation_id == action.obligation_id)
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return session.scalar(statement)


def _same_owner(action, projection) -> bool:
    return ((projection.tenant_id, projection.task_id, projection.task_lifecycle_epoch, projection.active_action_id)
            == (action.tenant_id, action.task_id, action.task_lifecycle_epoch, action.id))
