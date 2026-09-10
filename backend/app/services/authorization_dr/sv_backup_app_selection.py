"""Keep the account's actual C App independent when replacing its SV backup."""
from dataclasses import dataclass

from sqlalchemy import select

from app.models import DeveloperAppSlotAssignment, TelegramDeveloperApp, TgAccountAuthorization
from .contracts import AuthorizationDrError

ASSIGNMENT_PREFERENCE = ('standby_1_sv', 'primary_sv', 'standby_2_my')


@dataclass(frozen=True, kw_only=True)
class PreservedC:
    authorization_id: int
    developer_app_id: int
    fact_version: int
    slot_generation: int
    region: str
    wake_bundle_id: str | None


def select_sv_backup_app(session, primary):
    preserved = _current_c(session, primary)
    excluded = {primary.developer_app_id}
    if preserved:
        excluded.add(preserved.developer_app_id)
    for purpose in ASSIGNMENT_PREFERENCE:
        assignment = session.get(DeveloperAppSlotAssignment, purpose, populate_existing=True)
        if not assignment or assignment.status != 'active':
            continue
        app = session.get(TelegramDeveloperApp, assignment.developer_app_id, populate_existing=True)
        if app and app.is_active and app.id not in excluded:
            return assignment, app, preserved
    raise AuthorizationDrError(
        'developer_app_slot_assignment_conflict',
        'No active Developer App is distinct from current A and the account C slot',
    )


def _current_c(session, primary):
    rows = list(session.scalars(select(TgAccountAuthorization).where(
        TgAccountAuthorization.tenant_id == primary.tenant_id,
        TgAccountAuthorization.account_id == primary.account_id,
        TgAccountAuthorization.logical_slot == 'standby_2',
        TgAccountAuthorization.is_slot_current.is_(True),
        TgAccountAuthorization.disabled_at.is_(None),
    ).execution_options(populate_existing=True)))
    if not rows:
        return None
    if len(rows) != 1:
        raise AuthorizationDrError('migration_source_standby_not_unique', 'Current C slot is not unique')
    c = rows[0]
    if not c.developer_app_id or c.developer_app_id == primary.developer_app_id:
        raise AuthorizationDrError('developer_app_slot_assignment_conflict', 'Current A/C Apps are not distinct')
    return PreservedC(authorization_id=c.id, developer_app_id=c.developer_app_id,
                      fact_version=c.fact_version, slot_generation=c.slot_generation,
                      region=c.provision_region_code, wake_bundle_id=c.wake_bundle_id)
