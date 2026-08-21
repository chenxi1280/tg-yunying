from __future__ import annotations

from app.models import AuthorizationDrRuntimeContract
from app.services._common import _now


def disarm_scoped_runtime(session, operation, *, actor: str) -> bool:
    contract = session.get(AuthorizationDrRuntimeContract, 1)
    if not contract or contract.claim_scope_operation_id != operation.id:
        return False
    contract.mode = "off"
    contract.claim_scope_operation_id = ""
    contract.contract_epoch += 1
    contract.version += 1
    contract.updated_by = actor
    contract.updated_at = _now()
    return True


__all__ = ["disarm_scoped_runtime"]
