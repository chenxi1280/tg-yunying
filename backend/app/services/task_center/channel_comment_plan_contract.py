from __future__ import annotations

from dataclasses import dataclass, replace

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ChannelCommentEligibleAccountSnapshotRow,
    ChannelCommentGroundingAssignment,
    ChannelCommentOrdinalAccountBinding,
    ChannelCommentPlanContract,
    ChannelCommentQualityTargetRevision,
    ChannelMessage,
    ChannelMessageSourceRevision,
    Task,
    TaskDayLedger,
    TaskParticipationUnitPlan,
    PlanningAdmissionSnapshot,
)

from .source_pacing import rolling_source_window
from .engagement_comment_participation import (
    CommentParticipationDecision,
    POLICY_REVISION as QUANTITY_CONTRACT_VERSION,
    account_ids_hash,
    business_max_comments,
    comment_participation_contract_fields,
    planned_fallback_max_bps,
    prepare_comment_participation,
)
from .channel_comment_quality_target import (
    build_quality_target_component,
    current_quality_target,
    freeze_initial_quality_target,
    quality_assignment_content,
)
from .channel_comment_discussion_guard import (
    DiscussionPlanIdentity,
    resolve_discussion_plan_identity,
)
from .channel_comment_plan_concurrency import active_plan_conflict
from .channel_comment_grounding_snapshot import (
    assignment_eligible_variants,
    GroundingSnapshotDraft,
    build_initial_grounding_draft,
    freeze_initial_grounding_snapshot,
)


@dataclass(frozen=True)
class FrozenCommentPlan:
    contract: ChannelCommentPlanContract
    quality_target: ChannelCommentQualityTargetRevision
    account_by_ordinal: dict[int, int]
    assignment_by_ordinal: dict[int, ChannelCommentGroundingAssignment]
    discussion_identity: DiscussionPlanIdentity | None = None


def grounding_plan_enabled(task: Task) -> bool:
    return bool((task.type_config or {}).get("channel_comment_grounding_v1_enabled"))


def ensure_comment_plan_contract(
    session: Session,
    task: Task,
    message: ChannelMessage,
    *,
    accounts: list,
    ledger: TaskDayLedger | None = None,
    participation_plan: TaskParticipationUnitPlan | None = None,
    admission_snapshot: PlanningAdmissionSnapshot | None = None,
) -> FrozenCommentPlan:
    existing = session.scalar(select(ChannelCommentPlanContract).where(
        ChannelCommentPlanContract.task_id == task.id,
        ChannelCommentPlanContract.channel_message_id == message.id,
        ChannelCommentPlanContract.contract_state == "open",
    ))
    if existing is not None:
        return _frozen_plan(session, existing)
    try:
        with session.begin_nested():
            return _create_comment_plan(
                session,
                task,
                message,
                accounts=accounts,
                ledger=ledger,
                daily_participation_plan=participation_plan,
                planning_admission_snapshot=admission_snapshot,
            )
    except IntegrityError as exc:
        if not active_plan_conflict(exc):
            raise
        winner = session.scalar(select(ChannelCommentPlanContract).where(
            ChannelCommentPlanContract.task_id == task.id,
            ChannelCommentPlanContract.channel_message_id == message.id,
            ChannelCommentPlanContract.contract_state == "open",
        ))
        if winner is None:
            raise
        return _frozen_plan(session, winner)


def _create_comment_plan(
    session: Session,
    task: Task,
    message: ChannelMessage,
    *,
    accounts: list,
    ledger: TaskDayLedger | None,
    daily_participation_plan: TaskParticipationUnitPlan | None,
    planning_admission_snapshot: PlanningAdmissionSnapshot | None,
) -> FrozenCommentPlan:
    source = _source_revision(session, task, message)
    if ledger is None and planning_admission_snapshot is not None:
        admitted = set(planning_admission_snapshot.admissible_account_ids or [])
        accounts = [account for account in accounts if int(account.id) in admitted]
    if not accounts:
        raise ValueError("planning_admission_blocked")
    discussion, participation = _participation_and_admission(
        session,
        task,
        message,
        source=source,
        accounts=accounts,
        ledger=ledger,
    )
    ranked = participation.ranked_accounts
    required = participation.required_count
    fallback_max_bps = planned_fallback_max_bps(task)
    window_start, deadline = rolling_source_window(task, source.source_published_at)
    grounding_draft, component = _initial_grounding_component(
        task, source, required=required, fallback_max_bps=fallback_max_bps,
        latest_safe_send_at=deadline,
    )
    contract = _new_plan_contract(
        task, message, source=source, ranked=ranked,
        participation=participation, fallback_max_bps=fallback_max_bps,
        required=required, window_start=window_start, deadline=deadline,
        grounding_required=int(component["grounding_required_count"]),
        discussion=discussion, ledger=ledger,
        daily_participation_plan=daily_participation_plan,
        planning_admission_snapshot=planning_admission_snapshot,
    )
    return _persist_comment_plan(
        session, task, contract=contract, source=source, component=component,
        ranked=ranked, required=required, discussion=discussion,
        grounding_draft=grounding_draft,
    )


def _participation_and_admission(
    session: Session,
    task: Task,
    message: ChannelMessage,
    *,
    source: ChannelMessageSourceRevision,
    accounts: list,
    ledger: TaskDayLedger | None,
) -> tuple[DiscussionPlanIdentity, CommentParticipationDecision]:
    discussion = resolve_discussion_plan_identity(
        session, task, source, accounts=accounts,
    )
    eligible_ids = set(discussion.membership_by_account) | set(discussion.admission_candidate_ids)
    eligible = accounts if ledger is not None else [item for item in accounts if item.id in eligible_ids]
    participation = prepare_comment_participation(
        session,
        task,
        message,
        source=source,
        ledger=ledger,
        accounts=eligible,
        business_max=business_max_comments(task),
    )
    ranked = participation.ranked_accounts
    required = participation.required_count
    discussion = _reserve_selected_admissions(
        session, task, discussion=discussion,
        ranked=ranked, required=required,
    )
    return discussion, participation


def _initial_grounding_component(
    task: Task,
    source: ChannelMessageSourceRevision,
    *,
    required: int,
    fallback_max_bps: int,
    latest_safe_send_at,
) -> tuple[GroundingSnapshotDraft, dict]:
    draft = build_initial_grounding_draft(task, source)
    component = build_quality_target_component(
        source,
        list(range(1, required + 1)),
        comment_grounding_revision=1,
        planned_fallback_max_bps=fallback_max_bps,
        semantic_variant_units=assignment_eligible_variants(
            draft.facts, latest_safe_send_at=latest_safe_send_at,
        ),
    )
    return draft, component


def _reserve_selected_admissions(
    session: Session,
    task: Task,
    *,
    discussion: DiscussionPlanIdentity,
    ranked: list,
    required: int,
) -> DiscussionPlanIdentity:
    from datetime import datetime, timezone
    from .channel_comment_discussion_admission import ensure_discussion_membership_actions

    selected = [
        account for account in ranked[:required]
        if int(account.id) not in discussion.membership_by_account
    ]
    actions = ensure_discussion_membership_actions(
        session, task, discussion.group_binding,
        accounts=selected, now_value=datetime.now(timezone.utc),
    ) if selected else {}
    return replace(discussion, admission_action_by_account=actions)


def _persist_comment_plan(
    session: Session,
    task: Task,
    *,
    contract: ChannelCommentPlanContract,
    source: ChannelMessageSourceRevision,
    component: dict,
    ranked: list,
    required: int,
    discussion: DiscussionPlanIdentity,
    grounding_draft: GroundingSnapshotDraft,
) -> FrozenCommentPlan:
    session.add(contract)
    session.flush()
    grounding_snapshot = freeze_initial_grounding_snapshot(
        session, task, plan=contract, source=source, draft=grounding_draft,
    )
    component["grounding_snapshot_id"] = grounding_snapshot.id
    quality_target = freeze_initial_quality_target(
        session, contract, source, component=component,
    )
    _freeze_accounts(
        session, task, contract=contract, accounts=ranked, required=required,
        discussion=discussion,
    )
    _freeze_grounding_assignments(
        session, task, contract=contract, source=source,
        quality_target=quality_target, grounding_snapshot=grounding_snapshot,
    )
    return _frozen_plan(session, contract)


def _new_plan_contract(
    task: Task,
    message: ChannelMessage,
    *,
    source: ChannelMessageSourceRevision,
    ranked: list,
    participation: CommentParticipationDecision,
    fallback_max_bps: int,
    required: int,
    window_start,
    deadline,
    grounding_required: int,
    discussion: DiscussionPlanIdentity,
    ledger: TaskDayLedger | None,
    daily_participation_plan: TaskParticipationUnitPlan | None,
    planning_admission_snapshot: PlanningAdmissionSnapshot | None,
) -> ChannelCommentPlanContract:
    return ChannelCommentPlanContract(
        tenant_id=task.tenant_id,
        task_id=task.id,
        **comment_participation_contract_fields(
            participation, ledger=ledger, daily_plan=daily_participation_plan
        ),
        channel_message_id=message.id,
        planning_admission_snapshot_id=(
            planning_admission_snapshot.id if planning_admission_snapshot else None
        ),
        comment_plan_revision=1,
        source_revision_id=source.id,
        source_journey_plan_id=participation.journey_plan_id or None,
        source_published_at=source.source_published_at,
        source_observed_at=source.source_observed_at,
        window_start_at=window_start,
        deadline_at=deadline,
        eligible_account_ids_hash=account_ids_hash(ranked),
        planned_fallback_max_bps=fallback_max_bps,
        grounding_required_count=grounding_required,
        planned_fallback_count=required - grounding_required,
        grounding_enrollment_id=discussion.enrollment.id,
        discussion_group_binding_id=discussion.group_binding.id,
        discussion_group_binding_revision=discussion.group_binding.binding_revision,
        discussion_group_identity_hash=discussion.group_binding.identity_hash,
        discussion_thread_binding_id=discussion.thread_binding.id,
        discussion_thread_revision=discussion.thread_binding.thread_revision,
        discussion_thread_identity_hash=discussion.thread_binding.identity_hash,
        daily_comment_cap=int((task.type_config or {}).get("daily_comment_cap") or 0),
        quantity_contract_version=QUANTITY_CONTRACT_VERSION,
        contract_state="open",
    )
def _source_revision(
    session: Session,
    task: Task,
    message: ChannelMessage,
) -> ChannelMessageSourceRevision:
    source = (
        session.get(ChannelMessageSourceRevision, message.current_source_revision_id)
        if message.current_source_revision_id
        else None
    )
    if source is None or source.channel_message_id != message.id:
        task.last_error = "source_revision_unproven"
        raise ValueError("source_revision_unproven")
    return source


def _freeze_accounts(
    session: Session,
    task: Task,
    *,
    contract: ChannelCommentPlanContract,
    accounts: list,
    required: int,
    discussion: DiscussionPlanIdentity,
) -> None:
    for rank, account in enumerate(accounts, 1):
        session.add(ChannelCommentEligibleAccountSnapshotRow(
            tenant_id=task.tenant_id,
            plan_contract_id=contract.id,
            account_id=account.id,
            eligibility_state="eligible",
            stable_rank=rank,
            eligibility_snapshot={
                "profile_sync_status": account.profile_sync_status,
                "membership_fact_id": getattr(
                    discussion.membership_by_account.get(int(account.id)), "id", "",
                ),
                "membership_action_id": getattr(
                    discussion.admission_action_by_account.get(int(account.id)), "id", "",
                ),
                "discussion_binding_id": discussion.group_binding.id,
                "readiness_state": (
                    "membership_ready"
                    if int(account.id) in discussion.membership_by_account
                    else (
                        "admission_reserved"
                        if int(account.id) in discussion.admission_action_by_account
                        else "admission_candidate" if int(account.id) in discussion.admission_candidate_ids
                        else "membership_blocked"
                    )
                ),
            },
        ))
        if rank <= required:
            session.add(ChannelCommentOrdinalAccountBinding(
                tenant_id=task.tenant_id,
                plan_contract_id=contract.id,
                target_ordinal=rank,
                binding_attempt=1,
                account_id=account.id,
                binding_state="active",
            ))
    session.flush()


def _freeze_grounding_assignments(
    session: Session,
    task: Task,
    *,
    contract: ChannelCommentPlanContract,
    source: ChannelMessageSourceRevision,
    quality_target: ChannelCommentQualityTargetRevision,
    grounding_snapshot: object,
) -> None:
    component = quality_target.component_targets_json[0]
    for ordinal in component["grounding_ordinal_ids"]:
        session.add(ChannelCommentGroundingAssignment(
            tenant_id=task.tenant_id,
            plan_contract_id=contract.id,
            source_revision_id=source.id,
            grounding_snapshot_id=grounding_snapshot.id,
            comment_grounding_revision=grounding_snapshot.comment_grounding_revision,
            target_ordinal=ordinal,
            assignment_version=1,
            quality_target_revision_id=quality_target.id,
            quality_component_key=component["quality_component_key"],
            **quality_assignment_content(source, component, int(ordinal)),
            assignment_state="active",
        ))
    session.flush()


def _account_bindings(session: Session, plan_contract_id: str) -> dict[int, int]:
    rows = session.execute(select(
        ChannelCommentOrdinalAccountBinding.target_ordinal,
        ChannelCommentOrdinalAccountBinding.account_id,
    ).where(
        ChannelCommentOrdinalAccountBinding.plan_contract_id == plan_contract_id,
        ChannelCommentOrdinalAccountBinding.binding_state == "active",
    ))
    return {int(ordinal): int(account_id) for ordinal, account_id in rows}


def _grounding_assignments(
    session: Session,
    plan_contract_id: str,
) -> dict[int, ChannelCommentGroundingAssignment]:
    rows = session.scalars(select(ChannelCommentGroundingAssignment).where(
        ChannelCommentGroundingAssignment.plan_contract_id == plan_contract_id,
        ChannelCommentGroundingAssignment.assignment_state == "active",
    ))
    return {int(row.target_ordinal): row for row in rows}


def _frozen_plan(session: Session, contract: ChannelCommentPlanContract) -> FrozenCommentPlan:
    discussion = _discussion_identity_for_contract(session, contract)
    return FrozenCommentPlan(
        contract,
        current_quality_target(session, contract),
        _account_bindings(session, contract.id),
        _grounding_assignments(session, contract.id),
        discussion,
    )


def _discussion_identity_for_contract(
    session: Session,
    contract: ChannelCommentPlanContract,
) -> DiscussionPlanIdentity | None:
    if not contract.grounding_enrollment_id:
        return None
    from app.models import (
        ChannelCommentGroundingEnrollment,
        ChannelDiscussionGroupBinding,
        ChannelDiscussionThreadBinding,
        DiscussionMembershipFact,
    )

    enrollment = session.get(ChannelCommentGroundingEnrollment, contract.grounding_enrollment_id)
    binding = session.get(ChannelDiscussionGroupBinding, contract.discussion_group_binding_id)
    thread = session.get(ChannelDiscussionThreadBinding, contract.discussion_thread_binding_id)
    rows = list(session.scalars(select(ChannelCommentEligibleAccountSnapshotRow).where(
        ChannelCommentEligibleAccountSnapshotRow.plan_contract_id == contract.id,
    )))
    facts = _current_plan_memberships(session, binding, rows) if binding else {}
    admissions = {
        row.account_id: session.get(Action, row.eligibility_snapshot.get("membership_action_id"))
        for row in rows if row.eligibility_snapshot.get("membership_action_id")
    }
    admissions = {key: value for key, value in admissions.items() if value is not None}
    if enrollment is None or binding is None or thread is None:
        raise ValueError("channel_comment_frozen_discussion_identity_missing")
    candidate_ids = frozenset(
        row.account_id for row in rows
        if row.eligibility_snapshot.get("readiness_state") in {
            "admission_candidate", "admission_reserved",
        }
    )
    return DiscussionPlanIdentity(
        enrollment, binding, thread, facts, admissions, candidate_ids,
        freeze_pending_memberships=contract.task_day_ledger_id is not None,
    )


def _current_plan_memberships(session: Session, binding, rows: list) -> dict:
    from datetime import datetime, timezone
    from .channel_comment_discussion_contracts import current_membership_fact, membership_ready

    now_value = datetime.now(timezone.utc)
    facts = {}
    for row in rows:
        fact = current_membership_fact(
            session,
            tenant_id=row.tenant_id,
            account_id=row.account_id,
            discussion_peer_id=str(binding.discussion_peer_id),
            group_binding_id=binding.id,
        )
        if membership_ready(fact, now_value):
            facts[row.account_id] = fact
    return facts


__all__ = [
    "FrozenCommentPlan",
    "QUANTITY_CONTRACT_VERSION",
    "ensure_comment_plan_contract",
    "grounding_plan_enabled",
]
