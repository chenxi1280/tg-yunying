from __future__ import annotations

from app.models import TgAccount, TgAccountAuthorization

from .abc_backup import _execute_b_login, _operation_result
from .contracts import AuthorizationDrError
from .online_abc_primary import primary_state
from .primary_fence import verified_code_source


def require_approved_abc_backup_no_effect(session, operation) -> None:
    valid = (
        operation
        and operation.operation_type == "provision_standby_1"
        and operation.status == "approved"
        and operation.remote_call_state == "none"
        and operation.remote_effect_started_at is None
        and operation.login_flow_id is None
        and operation.candidate_authorization_id is None
        and not operation.blocker_code
        and operation.reconcile_status == "none"
    )
    if not valid:
        raise AuthorizationDrError(
            "online_abc_resume_remote_effect_started",
            "B resume requires the original approved no-effect operation",
        )
    verified_code_source(session, operation, allow_unpersisted_identity=True)


def resume_approved_abc_backup(session, operation) -> dict:
    require_approved_abc_backup_no_effect(session, operation)
    _execute_b_login(session, operation)
    return _operation_result(operation)


def resume_approved_abc_backup_if_present(session, operation) -> None:
    if operation and operation.status == "approved":
        resume_approved_abc_backup(session, operation)


def require_pre_b_no_effect_resume(session, item, operations: dict) -> str:
    if operations["c"] is not None or operations["e4"] is not None:
        raise AuthorizationDrError(
            "online_abc_resume_remote_effect_started",
            "Pre-B resume requires no C/E4 operation",
        )
    account = session.get(TgAccount, item.account_id)
    primary = session.get(TgAccountAuthorization, item.primary_authorization_id)
    if primary_state(account, primary, item) not in {"frozen", "legacy_frozen"}:
        raise AuthorizationDrError("online_abc_primary_drift", "A changed before pre-B resume")
    require_approved_abc_backup_no_effect(session, operations["b"])
    return "pre_b_approved_no_remote_effect"


__all__ = [
    "require_approved_abc_backup_no_effect",
    "require_pre_b_no_effect_resume",
    "resume_approved_abc_backup",
    "resume_approved_abc_backup_if_present",
]
