from __future__ import annotations

from sqlalchemy import func, select

from app.models import (
    TgAccountAuthorization,
    TgAuthorizationDrOperation,
    TgAuthorizationWakeBundle,
)


def next_standby_2_generation(session, account_id: int) -> int:
    authorization_max = session.scalar(select(func.max(TgAccountAuthorization.slot_generation)).where(
        TgAccountAuthorization.account_id == account_id,
        TgAccountAuthorization.logical_slot == "standby_2",
    ))
    operation_max = session.scalar(select(func.max(TgAuthorizationDrOperation.target_generation)).where(
        TgAuthorizationDrOperation.account_id == account_id,
        TgAuthorizationDrOperation.logical_slot == "standby_2",
    ))
    bundle_max = session.scalar(select(func.max(TgAuthorizationWakeBundle.bundle_generation)).where(
        TgAuthorizationWakeBundle.account_id == account_id,
    ))
    return max(int(authorization_max or 0), int(operation_max or 0), int(bundle_max or 0)) + 1


__all__ = ["next_standby_2_generation"]
