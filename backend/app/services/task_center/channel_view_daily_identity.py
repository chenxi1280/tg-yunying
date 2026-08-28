from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ChannelViewDailyIdentityOwner,
    TaskDayLedger,
    ViewFulfillmentObligation,
    ViewRemoteFact,
)


@dataclass(frozen=True)
class DailyIdentityClaim:
    tenant_id: int
    logical_task_id: str
    target_peer_id: str
    channel_message_id: int
    account_id: int
    obligation_local_date: date
    obligation_id: str
    action_id: str | None = None

    @property
    def request_identity(self) -> str:
        return f"{self.logical_task_id}:{self.obligation_id}"


def claim_daily_identity(
    session: Session,
    claim: DailyIdentityClaim,
) -> ChannelViewDailyIdentityOwner | None:
    owner = _locked_owner(session, claim)
    if owner is None:
        owner = _insert_owner(session, claim)
    if owner is None:
        owner = _locked_owner(session, claim)
    if owner is None or not _claimable_by(owner, claim):
        return None
    _bind_claim(owner, claim)
    return owner


def bind_daily_identity_action(
    session: Session,
    obligation: ViewFulfillmentObligation,
    action: Action,
) -> ChannelViewDailyIdentityOwner:
    owner = session.scalar(
        select(ChannelViewDailyIdentityOwner)
        .where(ChannelViewDailyIdentityOwner.obligation_id == obligation.id)
        .with_for_update()
    )
    if owner is None or owner.state != "pre_gateway":
        raise ValueError("channel_view_daily_identity_owner_missing")
    if owner.logical_task_id != action.task_id:
        raise ValueError("channel_view_daily_identity_task_mismatch")
    owner.action_id = action.id
    owner.version += 1
    return owner


def mark_daily_identity_call_issued(session: Session, action: Action) -> None:
    owner = _owner_for_action(session, action)
    if owner.state == "call_issued":
        return
    if owner.state != "pre_gateway":
        raise ValueError(f"channel_view_daily_identity_not_pre_gateway:{owner.state}")
    owner.state = "call_issued"
    owner.version += 1


def mark_daily_identity_unknown(session: Session, action: Action) -> None:
    owner = _owner_for_action(session, action)
    if owner.state in {"unknown", "confirmed"}:
        return
    if owner.state not in {"pre_gateway", "call_issued"}:
        raise ValueError(f"channel_view_daily_identity_cannot_mark_unknown:{owner.state}")
    owner.state = "unknown"
    owner.version += 1


def release_daily_identity(session: Session, action: Action) -> bool:
    owner = _owner_for_action(session, action, required=False)
    if owner is None or owner.state != "pre_gateway":
        return False
    owner.state = "available"
    owner.logical_task_id = action.task_id
    owner.obligation_id = None
    owner.action_id = None
    owner.request_identity = f"released:{owner.id}:{owner.version + 1}"
    owner.version += 1
    return True


def release_claimed_identity(
    session: Session,
    obligation: ViewFulfillmentObligation,
) -> bool:
    owner = session.scalar(
        select(ChannelViewDailyIdentityOwner)
        .where(ChannelViewDailyIdentityOwner.obligation_id == obligation.id)
        .with_for_update()
    )
    if owner is None or owner.state != "pre_gateway" or owner.action_id is not None:
        return False
    owner.state = "available"
    owner.obligation_id = None
    owner.request_identity = f"released:{owner.id}:{owner.version + 1}"
    owner.version += 1
    return True


def confirm_daily_identity(
    session: Session,
    obligation: ViewFulfillmentObligation,
    fact: ViewRemoteFact,
) -> None:
    ledger = session.get(TaskDayLedger, obligation.task_day_ledger_id)
    if ledger is None:
        raise ValueError("view_obligation_ledger_missing")
    claim = _claim_from_fact(ledger, obligation, fact)
    owner = claim_daily_identity(session, claim)
    if owner is None:
        raise ValueError("channel_view_daily_identity_owned_by_another_task")
    owner.state = "confirmed"
    owner.action_id = obligation.current_action_id
    owner.version += 1


def _locked_owner(
    session: Session,
    claim: DailyIdentityClaim,
) -> ChannelViewDailyIdentityOwner | None:
    return session.scalar(_identity_statement(claim).with_for_update())


def _identity_statement(claim: DailyIdentityClaim):
    return select(ChannelViewDailyIdentityOwner).where(
        ChannelViewDailyIdentityOwner.target_peer_id == claim.target_peer_id,
        ChannelViewDailyIdentityOwner.channel_message_id == claim.channel_message_id,
        ChannelViewDailyIdentityOwner.account_id == claim.account_id,
        ChannelViewDailyIdentityOwner.obligation_local_date == claim.obligation_local_date,
    )


def _insert_owner(
    session: Session,
    claim: DailyIdentityClaim,
) -> ChannelViewDailyIdentityOwner | None:
    owner = ChannelViewDailyIdentityOwner(
        tenant_id=claim.tenant_id,
        target_peer_id=claim.target_peer_id,
        channel_message_id=claim.channel_message_id,
        account_id=claim.account_id,
        obligation_local_date=claim.obligation_local_date,
        state="pre_gateway",
        logical_task_id=claim.logical_task_id,
        obligation_id=claim.obligation_id,
        action_id=claim.action_id,
        request_identity=claim.request_identity,
    )
    try:
        with session.begin_nested():
            session.add(owner)
            session.flush()
    except IntegrityError:
        return None
    return owner


def _claimable_by(owner: ChannelViewDailyIdentityOwner, claim: DailyIdentityClaim) -> bool:
    if owner.tenant_id != claim.tenant_id:
        raise ValueError("channel_view_daily_identity_tenant_mismatch")
    if owner.state == "available":
        return True
    return (
        owner.obligation_id == claim.obligation_id
        and owner.logical_task_id == claim.logical_task_id
    )


def _bind_claim(owner: ChannelViewDailyIdentityOwner, claim: DailyIdentityClaim) -> None:
    if owner.state == "available":
        owner.state = "pre_gateway"
    owner.logical_task_id = claim.logical_task_id
    owner.obligation_id = claim.obligation_id
    if claim.action_id is not None:
        owner.action_id = claim.action_id
    owner.request_identity = claim.request_identity
    owner.version += 1


def _owner_for_action(
    session: Session,
    action: Action,
    *,
    required: bool = True,
) -> ChannelViewDailyIdentityOwner | None:
    owner = session.scalar(
        select(ChannelViewDailyIdentityOwner)
        .where(ChannelViewDailyIdentityOwner.action_id == action.id)
        .with_for_update()
    )
    if owner is None and required:
        raise ValueError("channel_view_daily_identity_action_owner_missing")
    return owner


def _claim_from_fact(
    ledger: TaskDayLedger,
    obligation: ViewFulfillmentObligation,
    fact: ViewRemoteFact,
) -> DailyIdentityClaim:
    return DailyIdentityClaim(
        tenant_id=obligation.tenant_id,
        logical_task_id=ledger.task_id,
        target_peer_id=fact.target_peer_id,
        channel_message_id=obligation.channel_message_id,
        account_id=obligation.account_id,
        obligation_local_date=ledger.obligation_local_date,
        obligation_id=obligation.id,
        action_id=obligation.current_action_id,
    )


__all__ = [
    "DailyIdentityClaim",
    "bind_daily_identity_action",
    "claim_daily_identity",
    "confirm_daily_identity",
    "mark_daily_identity_call_issued",
    "mark_daily_identity_unknown",
    "release_claimed_identity",
    "release_daily_identity",
]
