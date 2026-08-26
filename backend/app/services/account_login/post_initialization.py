from __future__ import annotations

from app.models import TgAccountFullInitialization
from app.services._common import _now
from app.services.account_post_login_init.binding import (
    create_or_attach_full_initialization,
    mark_login_authorized_waiting,
)
from app.services.account_post_login_init.parent import sync_parent_bindings
from app.services.account_post_login_init.policy import FULL_INIT_POLICY

from .state import PhaseClaim, commit_claim, fail_claim, load_claim, succeed_claim


def requires_full_initialization(item) -> bool:
    return item.initialization_policy == FULL_INIT_POLICY


def complete_online_readback(session_factory, claim: PhaseClaim, warning: str) -> None:
    with session_factory() as session:
        item, attempt = load_claim(session, claim)
        if not requires_full_initialization(item):
            succeed_claim(session, claim, warning=warning)
            commit_claim(session)
            return
        owner = create_or_attach_full_initialization(
            session,
            item,
            actor="account-login-worker",
        )
        item.warning_detail = warning
        mark_login_authorized_waiting(session, item, attempt, owner=owner)
        sync_parent_bindings(session, owner)
        commit_claim(session)


def fail_full_initialization_online_readback(
    session_factory,
    claim: PhaseClaim,
    detail: str,
) -> None:
    with session_factory() as session:
        item, _ = load_claim(session, claim)
        owner = session.get(TgAccountFullInitialization, item.post_initialization_id)
        if owner and owner.status == "waiting_login_parent":
            owner.status = "failed"
            owner.stage = "failed"
            owner.failure_type = "primary_online_readback_unproven"
            owner.failure_detail = detail[:500]
            owner.finished_at = _now()
            owner.version += 1
        fail_claim(session, claim, "primary_online_readback_unproven", detail[:500])
        commit_claim(session)


def attach_authorized_full_initialization(
    session,
    item,
    *,
    source_two_fa_kind: str,
    source_two_fa_password: str,
) -> None:
    if not requires_full_initialization(item):
        return
    create_or_attach_full_initialization(
        session,
        item,
        actor="account-login-worker",
        source_two_fa_kind=source_two_fa_kind,
        source_two_fa_password=source_two_fa_password,
    )
    item.authorization_status = "confirmed"


__all__ = [
    "attach_authorized_full_initialization",
    "complete_online_readback",
    "fail_full_initialization_online_readback",
    "requires_full_initialization",
]
