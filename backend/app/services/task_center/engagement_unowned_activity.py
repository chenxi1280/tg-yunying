from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    AccountBehaviorBudgetLedger,
    AccountBehaviorBudgetPolicyRevision,
    AccountExternalUseHold,
    Action,
    ExecutionAttempt,
    ExternalAccountUsePolicyRevision,
    TgAccount,
    TgAccountAuthorization,
    UnownedOutboundActivityObservation,
)
from app.services._common import _now
from app.timezone import as_beijing

from .engagement_runtime_settlement import move_counter
from .engagement_activity_scope import (
    action_activity_scope,
    payload_activity_source_identity,
)

SUPPORTED_ACTIVITY_CLASSES = frozenset(
    {"authored_message", "authored_comment", "reaction"}
)


def observe_managed_outbound(
    session: Session,
    *,
    tenant_id: int,
    canonical_peer_id: str,
    payload: dict,
    action_class: str,
    source_event_id: str = "",
) -> bool:
    _assert_supported_class(action_class)
    remote_id = str(payload.get("source_message_id") or "").strip()
    owned_account_id = _owned_account_id(
        session,
        tenant_id=tenant_id,
        canonical_peer_id=canonical_peer_id,
        remote_id=remote_id,
        action_class=action_class,
    )
    if owned_account_id is not None:
        return True
    account_id = _sender_account_id(
        session,
        tenant_id=tenant_id,
        payload=payload,
    )
    if account_id is None:
        return False
    remote_identity = remote_id or str(source_event_id or "")
    if not remote_identity:
        return True
    _record_unowned(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        canonical_peer_id=canonical_peer_id,
        remote_id=remote_identity,
        action_class=action_class,
        source_event_id=source_event_id,
        source_identity=payload_activity_source_identity(payload),
    )
    return True


def _owned_account_id(
    session: Session,
    *,
    tenant_id: int,
    canonical_peer_id: str,
    remote_id: str,
    action_class: str,
) -> int | None:
    if not remote_id:
        return None
    action_types = _action_types(action_class)
    rows = session.execute(
        select(Action, ExecutionAttempt)
        .join(ExecutionAttempt, ExecutionAttempt.action_id == Action.id)
        .where(
            Action.tenant_id == tenant_id,
            Action.action_type.in_(action_types),
            ExecutionAttempt.remote_message_id == remote_id,
        )
    )
    for action, _attempt in rows:
        if action_activity_scope(session, action).canonical_peer_id == canonical_peer_id:
            return int(action.account_id) if action.account_id else None
    return None


def _sender_account_id(
    session: Session,
    *,
    tenant_id: int,
    payload: dict,
) -> int | None:
    sender_id = str(payload.get("sender_peer_id") or "").strip()
    if sender_id:
        digest = hashlib.sha256(sender_id.encode()).hexdigest()
        account_ids = list(session.scalars(
            select(TgAccountAuthorization.account_id)
            .join(TgAccount, TgAccount.id == TgAccountAuthorization.account_id)
            .where(
                TgAccount.tenant_id == tenant_id,
                TgAccount.deleted_at.is_(None),
                TgAccountAuthorization.telegram_user_id_digest == digest,
            )
            .distinct()
            .limit(2)
        ))
        if len(account_ids) == 1:
            return int(account_ids[0])
    username = str(payload.get("sender_username") or "").strip().lstrip("@").lower()
    return _unique_username_account(session, tenant_id=tenant_id, username=username)


def _unique_username_account(
    session: Session,
    *,
    tenant_id: int,
    username: str,
) -> int | None:
    if not username:
        return None
    ids = list(session.scalars(select(TgAccount.id).where(
        TgAccount.tenant_id == tenant_id,
        TgAccount.deleted_at.is_(None),
        func.lower(TgAccount.username) == username,
    ).limit(2)))
    return int(ids[0]) if len(ids) == 1 else None


def _record_unowned(
    session: Session,
    *,
    tenant_id: int,
    account_id: int,
    canonical_peer_id: str,
    remote_id: str,
    action_class: str,
    source_event_id: str,
    source_identity: str,
) -> None:
    observed_at = _now()
    identity_hash = _identity_hash(
        tenant_id=tenant_id,
        account_id=account_id,
        peer_id=canonical_peer_id,
        remote_id=remote_id,
        action_class=action_class,
    )
    existing = session.scalar(select(
        UnownedOutboundActivityObservation
    ).where(
        UnownedOutboundActivityObservation.activity_identity_hash
        == identity_hash,
    ))
    if existing is not None:
        return
    policy = _external_use_policy(session, tenant_id)
    hold_seconds, collision_classes = _policy_decision(policy, action_class)
    observation, hold = _new_activity_models(
        tenant_id=tenant_id,
        account_id=account_id,
        canonical_peer_id=canonical_peer_id,
        remote_id=remote_id,
        action_class=action_class,
        source_event_id=source_event_id,
        source_identity=source_identity,
        identity_hash=identity_hash,
        observed_at=observed_at,
        policy=policy,
        hold_seconds=hold_seconds,
        collision_classes=collision_classes,
    )
    if _insert_activity_pair(session, observation=observation, hold=hold):
        _charge_behavior_budget(
            session, account_id=account_id, observed_at=observed_at,
            action_class=action_class,
        )


def _new_activity_models(
    *,
    tenant_id: int,
    account_id: int,
    canonical_peer_id: str,
    remote_id: str,
    action_class: str,
    source_event_id: str,
    source_identity: str,
    identity_hash: str,
    observed_at: datetime,
    policy: ExternalAccountUsePolicyRevision,
    hold_seconds: int,
    collision_classes: list[str],
) -> tuple[UnownedOutboundActivityObservation, AccountExternalUseHold]:
    observation_id = str(uuid4())
    observation = UnownedOutboundActivityObservation(
        id=observation_id, tenant_id=tenant_id, account_id=account_id,
        activity_class=action_class, canonical_peer_id=canonical_peer_id,
        canonical_source_identity=source_identity, remote_identity=remote_id,
        activity_identity_hash=identity_hash, source_event_id=source_event_id,
        ownership_evidence={"matched_action": False, "source": "telegram_update"},
        observed_at=observed_at,
    )
    hold = AccountExternalUseHold(
        tenant_id=tenant_id, account_id=account_id,
        observation_id=observation_id, policy_revision_id=policy.id,
        canonical_peer_id=canonical_peer_id,
        canonical_source_identity=source_identity, action_class=action_class,
        collision_action_classes=collision_classes, starts_at=observed_at,
        expires_at=observed_at + timedelta(seconds=hold_seconds),
    )
    return observation, hold


def _insert_activity_pair(
    session: Session,
    *,
    observation: UnownedOutboundActivityObservation,
    hold: AccountExternalUseHold,
) -> bool:
    try:
        with session.begin_nested():
            session.add_all([observation, hold])
            session.flush()
        return True
    except IntegrityError:
        return False


def _identity_hash(
    *,
    tenant_id: int,
    account_id: int,
    peer_id: str,
    remote_id: str,
    action_class: str,
) -> str:
    payload = json.dumps(
        [tenant_id, account_id, peer_id, remote_id, action_class],
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _action_types(action_class: str) -> tuple[str, ...]:
    values = {
        "authored_message": ("send_message",),
        "authored_comment": ("post_comment",),
        "reaction": ("like_message",),
    }
    if action_class not in values:
        raise ValueError("unowned_activity_class_unsupported")
    return values[action_class]


def _assert_supported_class(action_class: str) -> None:
    if action_class not in SUPPORTED_ACTIVITY_CLASSES:
        raise ValueError("unowned_activity_class_unsupported")


def _external_use_policy(
    session: Session,
    tenant_id: int,
) -> ExternalAccountUsePolicyRevision:
    policy = session.scalar(select(ExternalAccountUsePolicyRevision).where(
        ExternalAccountUsePolicyRevision.tenant_id == tenant_id,
        ExternalAccountUsePolicyRevision.state == "active",
    ))
    if policy is None:
        raise RuntimeError("external_account_use_policy_missing")
    return policy


def _policy_decision(
    policy: ExternalAccountUsePolicyRevision,
    action_class: str,
) -> tuple[int, list[str]]:
    hold_seconds = int((policy.hold_seconds_by_class or {}).get(action_class) or 0)
    collision_classes = list(
        (policy.collision_classes_by_class or {}).get(action_class) or []
    )
    if hold_seconds <= 0 or not collision_classes:
        raise RuntimeError("external_account_use_policy_class_unconfigured")
    if "view" in collision_classes:
        raise RuntimeError("external_account_use_policy_passive_view_forbidden")
    return hold_seconds, collision_classes


def _charge_behavior_budget(
    session: Session,
    *,
    account_id: int,
    observed_at: datetime,
    action_class: str,
) -> None:
    account = session.scalar(select(TgAccount).where(TgAccount.id == account_id)
        .with_for_update().execution_options(populate_existing=True))
    if account is None:
        raise RuntimeError("external_activity_account_missing")
    policy = session.scalar(select(AccountBehaviorBudgetPolicyRevision).where(
        AccountBehaviorBudgetPolicyRevision.tenant_id == account.tenant_id,
        AccountBehaviorBudgetPolicyRevision.account_class == account.account_identity,
        AccountBehaviorBudgetPolicyRevision.state == "active",
    ))
    if policy is None:
        raise RuntimeError("account_behavior_budget_policy_missing")
    ledger = _behavior_ledger(session, account=account, policy=policy, observed_at=observed_at)
    move_counter(ledger, action_class, old_state=None, new_state="unowned")


def _behavior_ledger(
    session: Session,
    *,
    account: TgAccount,
    policy: AccountBehaviorBudgetPolicyRevision,
    observed_at: datetime,
) -> AccountBehaviorBudgetLedger:
    task_day = as_beijing(observed_at).date()
    statement = select(AccountBehaviorBudgetLedger).where(
        AccountBehaviorBudgetLedger.tenant_id == account.tenant_id,
        AccountBehaviorBudgetLedger.account_id == account.id,
        AccountBehaviorBudgetLedger.task_day == task_day,
    )
    if session.get_bind().dialect.name != "sqlite":
        statement = statement.with_for_update()
    ledger = session.scalar(statement)
    if ledger is not None:
        return ledger
    ledger = AccountBehaviorBudgetLedger(
        tenant_id=account.tenant_id, account_id=account.id, task_day=task_day,
        policy_revision_id=policy.id, action_budgets=dict(policy.action_budgets or {}),
        counters={},
    )
    session.add(ledger)
    session.flush()
    return ledger


__all__ = ["SUPPORTED_ACTIVITY_CLASSES", "observe_managed_outbound"]
