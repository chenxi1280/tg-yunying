from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ChannelCommentEligibleAccountSnapshotRow,
    ChannelCommentGroundingAssignment,
    ChannelCommentOrdinalAccountBinding,
    ChannelCommentPlanContract,
    ChannelMessage,
    ChannelMessageSourceRevision,
    Task,
)

from .source_pacing import rolling_source_window
from .ai_generator import _extract_channel_post_aspects


QUANTITY_CONTRACT_VERSION = "channel_comment_participation_v1"
PARTICIPATION_MIN_BPS = 5500
PARTICIPATION_MAX_BPS = 6500
GROUNDING_TARGET_BPS = 8500
SPEECH_ACTS = ("reaction", "specific_question", "cautious_verification", "concise_agreement")


@dataclass(frozen=True)
class FrozenCommentPlan:
    contract: ChannelCommentPlanContract
    account_by_ordinal: dict[int, int]
    assignment_by_ordinal: dict[int, ChannelCommentGroundingAssignment]


def grounding_plan_enabled(task: Task) -> bool:
    return bool((task.type_config or {}).get("channel_comment_grounding_v1_enabled"))


def ensure_comment_plan_contract(
    session: Session,
    task: Task,
    message: ChannelMessage,
    *,
    accounts: list,
) -> FrozenCommentPlan:
    existing = session.scalar(select(ChannelCommentPlanContract).where(
        ChannelCommentPlanContract.task_id == task.id,
        ChannelCommentPlanContract.channel_message_id == message.id,
        ChannelCommentPlanContract.contract_state == "open",
    ))
    if existing is not None:
        return _frozen_plan(session, existing)
    source = _source_revision(session, task, message)
    _require_post_enrollment_source(task, source)
    ranked = _ranked_accounts(task, message, accounts)
    bps = _participation_bps(task, message)
    required = _required_count(len(ranked), bps)
    window_start, deadline = rolling_source_window(task, source.source_published_at)
    grounding_required = _grounding_required_count(required, source.source_text_snapshot)
    contract = _new_plan_contract(
        task,
        message,
        source=source,
        ranked=ranked,
        bps=bps,
        required=required,
        window_start=window_start,
        deadline=deadline,
        grounding_required=grounding_required,
    )
    session.add(contract)
    session.flush()
    _freeze_accounts(
        session, task, contract=contract, accounts=ranked, required=required,
    )
    _freeze_grounding_assignments(
        session, task, contract=contract, source=source,
    )
    return _frozen_plan(session, contract)


def _new_plan_contract(
    task: Task,
    message: ChannelMessage,
    *,
    source: ChannelMessageSourceRevision,
    ranked: list,
    bps: int,
    required: int,
    window_start,
    deadline,
    grounding_required: int,
) -> ChannelCommentPlanContract:
    return ChannelCommentPlanContract(
        tenant_id=task.tenant_id,
        task_id=task.id,
        channel_message_id=message.id,
        comment_plan_revision=1,
        source_revision_id=source.id,
        source_published_at=source.source_published_at,
        source_observed_at=source.source_observed_at,
        window_start_at=window_start,
        deadline_at=deadline,
        eligible_account_count=len(ranked),
        eligible_account_ids_hash=_account_ids_hash(ranked),
        participation_seed=_participation_seed(task, message),
        effective_participation_bps=bps,
        required_distinct_account_count=required,
        grounding_required_count=grounding_required,
        planned_fallback_count=required - grounding_required,
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


def _require_post_enrollment_source(
    task: Task,
    source: ChannelMessageSourceRevision,
) -> None:
    enrollment_at = task.scheduled_start or task.created_at
    if enrollment_at is None:
        return
    if source.source_published_at.replace(tzinfo=None) >= enrollment_at.replace(tzinfo=None):
        return
    task.last_error = "source_before_task_enrollment"
    raise ValueError("source_before_task_enrollment")


def _grounding_required_count(required: int, source_text: str) -> int:
    if required <= 0 or not source_text.strip():
        return 0
    return min(required, math.ceil(required * GROUNDING_TARGET_BPS / 10000))


def _ranked_accounts(task: Task, message: ChannelMessage, accounts: list) -> list:
    seed = _participation_seed(task, message)
    return sorted(
        accounts,
        key=lambda account: hashlib.sha256(f"{seed}:{account.id}".encode()).hexdigest(),
    )


def _participation_seed(task: Task, message: ChannelMessage) -> str:
    return f"{task.tenant_id}:{task.id}:{message.id}:1:{QUANTITY_CONTRACT_VERSION}"


def _participation_bps(task: Task, message: ChannelMessage) -> int:
    digest = hashlib.sha256(_participation_seed(task, message).encode()).digest()
    span = PARTICIPATION_MAX_BPS - PARTICIPATION_MIN_BPS + 1
    return PARTICIPATION_MIN_BPS + int.from_bytes(digest[:4], "big") % span


def _required_count(eligible_count: int, target_bps: int) -> int:
    if eligible_count <= 0:
        return 0
    return min(
        range(1, eligible_count + 1),
        key=lambda count: (abs(count * 10000 / eligible_count - target_bps), count),
    )


def _account_ids_hash(accounts: list) -> str:
    payload = json.dumps([int(account.id) for account in accounts], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _freeze_accounts(
    session: Session,
    task: Task,
    *,
    contract: ChannelCommentPlanContract,
    accounts: list,
    required: int,
) -> None:
    for rank, account in enumerate(accounts, 1):
        session.add(ChannelCommentEligibleAccountSnapshotRow(
            tenant_id=task.tenant_id,
            plan_contract_id=contract.id,
            account_id=account.id,
            eligibility_state="eligible",
            stable_rank=rank,
            eligibility_snapshot={"profile_sync_status": account.profile_sync_status},
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
) -> None:
    for ordinal in range(1, int(contract.grounding_required_count) + 1):
        session.add(ChannelCommentGroundingAssignment(
            tenant_id=task.tenant_id,
            plan_contract_id=contract.id,
            source_revision_id=source.id,
            target_ordinal=ordinal,
            assignment_version=1,
            **grounding_assignment_content(source, ordinal),
            assignment_state="active",
        ))
    session.flush()


def _assigned_aspect(source_text: str, aspects: list[dict], ordinal: int) -> tuple[str, str]:
    if not aspects:
        return "source_fact", source_text.strip()[:500]
    aspect = aspects[(ordinal - 1) % len(aspects)]
    matches = "/".join(str(item) for item in (aspect.get("matches") or [])[:3])
    return str(aspect.get("code") or "source_fact"), matches or str(aspect.get("label") or "")


def grounding_assignment_content(
    source: ChannelMessageSourceRevision,
    ordinal: int,
) -> dict:
    extracted = _extract_channel_post_aspects(source.source_text_snapshot)
    aspects = list(extracted.get("aspects") or [])
    code, text = _assigned_aspect(source.source_text_snapshot, aspects, ordinal)
    return {
        "evidence_text": source.source_text_snapshot,
        "evidence_hash": source.source_content_hash,
        "primary_aspect_code": code,
        "primary_aspect_text": text,
        "teacher_name": str(extracted.get("teacher_name") or ""),
        "speech_act": SPEECH_ACTS[(ordinal - 1) % len(SPEECH_ACTS)],
    }


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
    return FrozenCommentPlan(
        contract,
        _account_bindings(session, contract.id),
        _grounding_assignments(session, contract.id),
    )


__all__ = [
    "FrozenCommentPlan",
    "QUANTITY_CONTRACT_VERSION",
    "ensure_comment_plan_contract",
    "grounding_assignment_content",
    "grounding_plan_enabled",
]
