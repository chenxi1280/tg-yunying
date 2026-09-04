from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import (
    AccountFleetActivityFactProjection,
    AccountFleetActivityLedger,
    AccountFleetActivityPolicyRevision,
    AccountGroupMembershipSnapshotSet,
    AccountPoolConcurrencyLease,
    Action,
    ConversationTurnClaim,
    FulfillmentFactProjectionState,
    FulfillmentRemoteFact,
    InteractionOpportunity,
    PostSendVisibilityObservation,
    ReactionFulfillmentObligation,
    ReactionRemoteFact,
    Task,
    TaskDayLedger,
    ViewFulfillmentObligation,
    ViewRemoteFact,
)
from app.timezone import BEIJING_TZ
from app.services._common import _now


ACTIVITY_CLASSES = (
    "passive_operation",
    "visible_reaction",
    "authored_content",
    "human_linked_interaction",
)
UNION_CLASS = "any_confirmed_business_operation"
CLASSIFICATION_POLICY_REVISION = "fleet_activity_classification_v1"
DEFAULT_REQUIRED_CLASSES = (UNION_CLASS,)
DEFAULT_CLASS_TARGETS = {
    UNION_CLASS: {"minimum_facts": 1, "window_days": 3},
    **{item: {"minimum_facts": 0, "window_days": 3} for item in ACTIVITY_CLASSES},
}


def ensure_fleet_activity_policy(
    session: Session,
    *,
    tenant_id: int,
    account_pool_id: int,
) -> AccountFleetActivityPolicyRevision:
    policy = session.scalar(select(AccountFleetActivityPolicyRevision).where(
        AccountFleetActivityPolicyRevision.tenant_id == tenant_id,
        AccountFleetActivityPolicyRevision.account_pool_id == account_pool_id,
        AccountFleetActivityPolicyRevision.state == "active",
    ))
    if policy is not None:
        return policy
    policy = AccountFleetActivityPolicyRevision(
        tenant_id=tenant_id,
        account_pool_id=account_pool_id,
        revision=1,
        rolling_window_days=3,
        required_activity_classes=list(DEFAULT_REQUIRED_CLASSES),
        class_targets=dict(DEFAULT_CLASS_TARGETS),
        classification_policy_revision=CLASSIFICATION_POLICY_REVISION,
    )
    session.add(policy)
    session.flush()
    return policy


def project_operation_fact(
    session: Session,
    fact: ReactionRemoteFact | ViewRemoteFact,
) -> bool:
    resolved = _operation_fact_context(session, fact)
    if resolved is None:
        return False
    action, task, activity_class = resolved
    return _project_action_activity(
        session,
        action,
        task,
        activity_class=activity_class,
        source_fact_kind=type(fact).__name__,
        source_fact_id=fact.id,
        observed_at=fact.remote_confirmed_at,
        evidence={"remote_fact_id": fact.id},
    )


def project_visible_authored_action(session: Session, action: Action) -> int:
    task = session.get(Task, action.task_id)
    if task is None or not _unified(task):
        return 0
    observation = _visible_observation(session, action)
    if observation is None:
        return 0
    projected = int(_project_action_activity(
        session,
        action,
        task,
        activity_class="authored_content",
        source_fact_kind="PostSendVisibilityObservation",
        source_fact_id=observation.id,
        observed_at=observation.checked_at or observation.created_at,
        evidence={"visibility_observation_id": observation.id},
    ))
    projected += int(_project_human_linked(
        session,
        action,
        task=task,
        observation=observation,
    ))
    return projected


def project_fulfillment_fact_activity(
    session: Session,
    fact: FulfillmentRemoteFact,
) -> int:
    state = _fleet_projection_state(session, fact.fact_id)
    if state is None or state.state == "projected":
        return 0
    try:
        with session.begin_nested():
            projected = _project_fulfillment_fact(session, fact)
    except Exception as exc:  # noqa: BLE001 - durable projection failure is retried
        _mark_projection_failed(state, exc)
        return 0
    _mark_projection_complete(state)
    return projected


def recover_fleet_activity_projections(
    session: Session,
    *,
    limit: int = 100,
) -> int:
    states = list(session.scalars(
        select(FulfillmentFactProjectionState)
        .where(
            FulfillmentFactProjectionState.projection_kind == "fleet_activity",
            FulfillmentFactProjectionState.state.in_(("pending", "failed")),
            FulfillmentFactProjectionState.next_retry_at <= _now(),
        )
        .order_by(FulfillmentFactProjectionState.next_retry_at.asc())
        .limit(max(1, int(limit)))
    ))
    completed = 0
    for state in states:
        fact = session.get(FulfillmentRemoteFact, state.fact_id)
        if fact is None:
            _mark_projection_complete(state)
            completed += 1
            continue
        before = state.state
        project_fulfillment_fact_activity(session, fact)
        completed += int(before != "projected" and state.state == "projected")
    return completed


def project_action_fleet_activity(session: Session, action: Action) -> int:
    facts = list(session.scalars(
        select(FulfillmentRemoteFact).where(
            FulfillmentRemoteFact.action_id == action.id,
            FulfillmentRemoteFact.fact_kind.in_((
                "view_observed", "reaction_observed", "remote_message_observed",
            )),
        )
    ))
    return sum(project_fulfillment_fact_activity(session, fact) for fact in facts)


def fleet_activity_selection_debt(
    session: Session,
    tenant_id: int,
    account_ids: tuple[int, ...],
    *,
    as_of: date,
    window_days: int,
) -> dict[int, tuple[int, int]]:
    ids = {int(item) for item in account_ids}
    if not ids:
        return {}
    period_start = as_of - timedelta(days=max(1, int(window_days)) - 1)
    ledgers = session.scalars(select(AccountFleetActivityLedger).where(
        AccountFleetActivityLedger.tenant_id == tenant_id,
        AccountFleetActivityLedger.account_id.in_(ids),
        AccountFleetActivityLedger.period_start >= period_start,
        AccountFleetActivityLedger.period_start <= as_of,
    ))
    totals = {account_id: 0 for account_id in ids}
    for ledger in ledgers:
        totals[ledger.account_id] += sum(
            int(value or 0) for value in (ledger.activity_counts or {}).values()
        )
    return {
        account_id: (int(total > 0), total)
        for account_id, total in totals.items()
    }


def _project_fulfillment_fact(session: Session, fact: FulfillmentRemoteFact) -> int:
    action = session.get(Action, fact.action_id)
    if action is None:
        raise RuntimeError("fleet_activity_remote_fact_action_missing")
    if fact.fact_kind == "remote_message_observed":
        return project_visible_authored_action(session, action)
    operation = _typed_operation_fact(session, action, fact.fact_kind)
    if operation is None:
        return 0
    return int(project_operation_fact(session, operation))


def _typed_operation_fact(session: Session, action: Action, fact_kind: str):
    if fact_kind == "reaction_observed":
        obligation = session.scalar(select(ReactionFulfillmentObligation).where(
            ReactionFulfillmentObligation.current_action_id == action.id,
        ))
        model = ReactionRemoteFact
    elif fact_kind == "view_observed":
        obligation = session.scalar(select(ViewFulfillmentObligation).where(
            ViewFulfillmentObligation.current_action_id == action.id,
        ))
        model = ViewRemoteFact
    else:
        return None
    if obligation is None:
        raise RuntimeError("fleet_activity_typed_obligation_missing")
    typed_fact = session.scalar(select(model).where(
        model.obligation_id == obligation.id,
    ))
    if typed_fact is None:
        raise RuntimeError("fleet_activity_typed_remote_fact_missing")
    return typed_fact


def _fleet_projection_state(session: Session, fact_id: str):
    return session.scalar(select(FulfillmentFactProjectionState).where(
        FulfillmentFactProjectionState.fact_id == fact_id,
        FulfillmentFactProjectionState.projection_kind == "fleet_activity",
    ))


def _mark_projection_complete(state) -> None:
    state.state = "projected"
    state.last_error = ""
    state.projected_at = _now()
    state.updated_at = _now()


def _mark_projection_failed(state, exc: Exception) -> None:
    state.state = "failed"
    state.last_error = f"{type(exc).__name__}:{exc}"[:2000]
    state.next_retry_at = _now() + timedelta(seconds=60)
    state.updated_at = _now()


def _project_human_linked(
    session: Session,
    action: Action,
    *,
    task: Task,
    observation: PostSendVisibilityObservation,
) -> bool:
    claim = session.scalar(select(ConversationTurnClaim).where(
        ConversationTurnClaim.action_id == action.id,
        ConversationTurnClaim.state == "served",
    ))
    if claim is None:
        return False
    opportunity = session.get(InteractionOpportunity, claim.interaction_opportunity_id)
    if opportunity is None or opportunity.relation_kind != "native_reply_external_human":
        return False
    return _project_action_activity(
        session,
        action,
        task,
        activity_class="human_linked_interaction",
        source_fact_kind="ConversationTurnClaim",
        source_fact_id=claim.id,
        observed_at=claim.settled_at or observation.checked_at or observation.created_at,
        evidence={"claim_id": claim.id, "opportunity_id": opportunity.id},
    )


def _operation_fact_context(session: Session, fact):
    if isinstance(fact, ReactionRemoteFact):
        obligation = session.get(ReactionFulfillmentObligation, fact.obligation_id)
        task = session.get(Task, obligation.task_id) if obligation else None
        action_id = obligation.current_action_id if obligation else None
        activity_class = "visible_reaction"
    else:
        obligation = session.get(ViewFulfillmentObligation, fact.obligation_id)
        ledger = session.get(TaskDayLedger, obligation.task_day_ledger_id) if obligation else None
        task = session.get(Task, ledger.task_id) if ledger else None
        action_id = obligation.current_action_id if obligation else None
        activity_class = "passive_operation"
    if task is None or not _unified(task):
        return None
    action = session.get(Action, action_id) if action_id else None
    if action is None or action.account_id != fact.account_id:
        raise RuntimeError("fleet_activity_action_provenance_missing")
    return action, task, activity_class


def _project_action_activity(
    session: Session,
    action: Action,
    task: Task,
    *,
    activity_class: str,
    source_fact_kind: str,
    source_fact_id: str,
    observed_at: datetime,
    evidence: dict,
) -> bool:
    if activity_class not in ACTIVITY_CLASSES or action.account_id is None:
        raise ValueError("fleet_activity_class_or_account_invalid")
    pool_id = _action_pool_id(session, action)
    policy = _active_policy(session, task.tenant_id, pool_id)
    ledger = _ensure_ledger(
        session,
        policy,
        account_id=action.account_id,
        observed_at=observed_at,
    )
    inserted = _insert_projection(
        session,
        ledger,
        task_id=task.id,
        action_id=action.id,
        activity_class=activity_class,
        source_fact_kind=source_fact_kind,
        source_fact_id=source_fact_id,
        observed_at=observed_at,
        evidence=evidence,
    )
    if inserted:
        _increment_ledger(
            ledger,
            policy,
            activity_class=activity_class,
            observed_at=observed_at,
        )
    return inserted


def _action_pool_id(session: Session, action: Action) -> int:
    lease_ids = set(session.scalars(select(AccountPoolConcurrencyLease.account_pool_id).where(
        AccountPoolConcurrencyLease.action_id == action.id,
        AccountPoolConcurrencyLease.account_id == action.account_id,
    )))
    if len(lease_ids) == 1:
        return int(next(iter(lease_ids)))
    if len(lease_ids) > 1:
        raise RuntimeError("fleet_activity_multiple_pool_provenance")
    return _snapshot_pool_id(session, action)


def _snapshot_pool_id(session: Session, action: Action) -> int:
    snapshots = session.scalars(select(AccountGroupMembershipSnapshotSet).where(
        AccountGroupMembershipSnapshotSet.task_id == action.task_id,
        AccountGroupMembershipSnapshotSet.task_lifecycle_epoch
        == action.task_lifecycle_epoch,
    ))
    pool_ids = {
        int(pool_id)
        for snapshot in snapshots
        if (pool_id := (snapshot.account_origin_groups or {}).get(str(action.account_id)))
    }
    if len(pool_ids) != 1:
        raise RuntimeError("fleet_activity_pool_provenance_missing")
    return next(iter(pool_ids))


def _active_policy(session: Session, tenant_id: int, pool_id: int):
    policy = session.scalar(
        select(AccountFleetActivityPolicyRevision)
        .where(
            AccountFleetActivityPolicyRevision.tenant_id == tenant_id,
            AccountFleetActivityPolicyRevision.account_pool_id == pool_id,
            AccountFleetActivityPolicyRevision.state == "active",
        )
        .with_for_update()
    )
    if policy is None:
        raise RuntimeError("fleet_activity_policy_missing_before_remote_fact")
    return policy


def _ensure_ledger(
    session: Session,
    policy,
    *,
    account_id: int,
    observed_at: datetime,
):
    period = _beijing_date(observed_at)
    values = {
        "id": str(uuid4()),
        "tenant_id": policy.tenant_id,
        "account_pool_id": policy.account_pool_id,
        "account_id": account_id,
        "policy_revision_id": policy.id,
        "period_kind": policy.period_kind,
        "period_start": period,
        "period_end": period,
        "activity_counts": {},
        "latest_activity_at": {},
        "qualified_activity_classes": [],
        "required_status": {},
        "fairness_debt": {},
    }
    _insert_do_nothing(
        session,
        AccountFleetActivityLedger,
        values=values,
        columns=(
            "tenant_id", "account_pool_id", "account_id",
            "period_start", "period_end",
        ),
    )
    ledger = session.scalar(
        select(AccountFleetActivityLedger)
        .where(
            AccountFleetActivityLedger.tenant_id == policy.tenant_id,
            AccountFleetActivityLedger.account_pool_id == policy.account_pool_id,
            AccountFleetActivityLedger.account_id == account_id,
            AccountFleetActivityLedger.period_start == period,
            AccountFleetActivityLedger.period_end == period,
        )
        .with_for_update()
    )
    if ledger is None:
        raise RuntimeError("fleet_activity_ledger_insert_missing")
    return ledger


def _insert_projection(
    session: Session,
    ledger,
    *,
    task_id: str,
    action_id: str,
    activity_class: str,
    source_fact_kind: str,
    source_fact_id: str,
    observed_at: datetime,
    evidence: dict,
) -> bool:
    values = {
        "id": str(uuid4()),
        "tenant_id": ledger.tenant_id,
        "account_pool_id": ledger.account_pool_id,
        "account_id": ledger.account_id,
        "ledger_id": ledger.id,
        "policy_revision_id": ledger.policy_revision_id,
        "task_id": task_id,
        "action_id": action_id,
        "activity_class": activity_class,
        "source_fact_kind": source_fact_kind,
        "source_fact_id": source_fact_id,
        "evidence": evidence,
        "observed_at": observed_at,
    }
    return _insert_do_nothing(
        session,
        AccountFleetActivityFactProjection,
        values=values,
        columns=(
            "account_pool_id", "account_id", "activity_class",
            "source_fact_kind", "source_fact_id",
        ),
    ) == 1


def _insert_do_nothing(
    session,
    model,
    *,
    values: dict,
    columns: tuple[str, ...],
) -> int:
    dialect = session.get_bind().dialect.name
    insert = sqlite_insert if dialect == "sqlite" else pg_insert
    statement = insert(model).values(**values).on_conflict_do_nothing(
        index_elements=list(columns)
    )
    return int(session.execute(statement).rowcount or 0)


def _increment_ledger(
    ledger,
    policy,
    *,
    activity_class: str,
    observed_at: datetime,
) -> None:
    counts = dict(ledger.activity_counts or {})
    counts[activity_class] = int(counts.get(activity_class) or 0) + 1
    latest = dict(ledger.latest_activity_at or {})
    latest[activity_class] = observed_at.isoformat()
    qualified = sorted(item for item, count in counts.items() if int(count) > 0)
    ledger.activity_counts = counts
    ledger.latest_activity_at = latest
    ledger.qualified_activity_classes = qualified
    ledger.required_status = _required_status(policy, counts)
    ledger.fairness_debt = {
        item: int(not ledger.required_status.get(item, False))
        for item in policy.required_activity_classes or []
    }
    ledger.updated_at = datetime.now(timezone.utc)


def _required_status(policy, counts: dict) -> dict:
    has_any = any(int(counts.get(item) or 0) > 0 for item in ACTIVITY_CLASSES)
    status = {UNION_CLASS: has_any}
    status.update({item: int(counts.get(item) or 0) > 0 for item in ACTIVITY_CLASSES})
    return {
        item: bool(status.get(item, False))
        for item in policy.required_activity_classes or []
    }


def _visible_observation(session: Session, action: Action):
    return session.scalar(
        select(PostSendVisibilityObservation)
        .where(
            PostSendVisibilityObservation.action_id == action.id,
            PostSendVisibilityObservation.state == "visible_confirmed",
        )
        .order_by(PostSendVisibilityObservation.checked_at.desc())
        .limit(1)
    )


def _beijing_date(value: datetime) -> date:
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return aware.astimezone(BEIJING_TZ).date()


def _unified(task: Task) -> bool:
    return str((task.type_config or {}).get("engagement_contract_version") or "") == (
        "unified_engagement_v1"
    )


__all__ = [
    "ACTIVITY_CLASSES",
    "CLASSIFICATION_POLICY_REVISION",
    "DEFAULT_CLASS_TARGETS",
    "DEFAULT_REQUIRED_CLASSES",
    "UNION_CLASS",
    "ensure_fleet_activity_policy",
    "fleet_activity_selection_debt",
    "project_action_fleet_activity",
    "project_fulfillment_fact_activity",
    "project_operation_fact",
    "project_visible_authored_action",
    "recover_fleet_activity_projections",
]
