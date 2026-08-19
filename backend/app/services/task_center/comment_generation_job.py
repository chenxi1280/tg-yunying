from __future__ import annotations

import hashlib
import json
from datetime import datetime

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import Action, GenerationJob
from app.services._common import _now

from .ai_generation_parallel import OPEN_GENERATION_JOB_PREDICATE
from .ai_generation_timing import GENERATION_LEASE
from .datetime_compat import is_after_or_equal
from .ai_content_runtime import defer_generation_job
from .generation_wait import latest_safe_send_at


COMMENT_GENERATION_OBLIGATION_TYPE = "post_comment"
_FINISHABLE_STATES = ("ready", "failed", "unknown")


class CommentGenerationJobConflict(RuntimeError):
    """open GenerationJob 被其他 owner 持有且租约未到期；按 typed 冲突上抛。"""


def comment_generation_obligation_id(action: Action, payload) -> str:
    """评论生成审计的稳定义务身份：优先 CommentFulfillmentObligation，回退 Action。"""
    obligation_id = str(getattr(payload, "comment_fulfillment_obligation_id", "") or "")
    return obligation_id or str(action.id)


def claim_comment_generation_job(
    session: Session,
    action: Action,
    payload,
    *,
    owner: str,
) -> GenerationJob:
    obligation_id = comment_generation_obligation_id(action, payload)
    sequence = _next_generation_sequence(session, obligation_id)
    _upsert_generation_job(
        session,
        _generation_job_values(
            session,
            action,
            payload,
            obligation_id=obligation_id,
            sequence=sequence,
        ),
    )
    job = _open_generation_job(session, obligation_id)
    if job is None:
        raise RuntimeError("comment_generation_job_missing")
    _claim_generation_job(session, job, owner=owner)
    session.refresh(job)
    return job


def _generation_job_values(
    session: Session,
    action: Action,
    payload,
    *,
    obligation_id: str,
    sequence: int,
) -> dict:
    return {
        "tenant_id": action.tenant_id,
        "task_id": action.task_id,
        "task_lifecycle_epoch": int(action.task_lifecycle_epoch or 1),
        "obligation_type": COMMENT_GENERATION_OBLIGATION_TYPE,
        "obligation_id": obligation_id,
        "generation_sequence": sequence,
        "context_snapshot_version": _context_version(payload),
        "generation_not_before_at": (
            action.effective_claim_at
            or action.release_not_before_at
            or action.scheduled_at
        ),
        "latest_safe_send_at": latest_safe_send_at(session, action),
        "context_snapshot_hash": _context_hash(payload),
        "assignment_revision": int(action.assignment_revision or 1),
        "intent_revision": int(action.intent_revision or 1),
        "candidate_hash": str(action.candidate_hash or ""),
        "evaluator_evidence": dict(
            (action.result or {}).get("evaluator_evidence") or {},
        ),
        "state": "pending",
    }


def _next_generation_sequence(session: Session, obligation_id: str) -> int:
    count = session.scalar(select(func.count(GenerationJob.id)).where(
        GenerationJob.obligation_type == COMMENT_GENERATION_OBLIGATION_TYPE,
        GenerationJob.obligation_id == obligation_id,
    ))
    return int(count or 0) + 1


def _upsert_generation_job(session: Session, values: dict) -> None:
    table = GenerationJob.__table__
    statement = pg_insert(table) if session.get_bind().dialect.name == "postgresql" else sqlite_insert(table)
    session.execute(statement.values(**values).on_conflict_do_nothing(
        index_elements=["obligation_type", "obligation_id"],
        index_where=text(OPEN_GENERATION_JOB_PREDICATE),
    ))


def _open_generation_job(
    session: Session,
    obligation_id: str,
) -> GenerationJob | None:
    return session.scalar(select(GenerationJob).where(
        GenerationJob.obligation_type == COMMENT_GENERATION_OBLIGATION_TYPE,
        GenerationJob.obligation_id == obligation_id,
        GenerationJob.state.in_(("pending", "generating", "unknown")),
    ))


def _claim_generation_job(
    session: Session,
    job: GenerationJob,
    *,
    owner: str,
) -> None:
    now_value = _now()
    if job.state == "generating" and job.generation_owner_id and job.generation_owner_id != owner:
        if job.lease_expires_at is not None and not is_after_or_equal(now_value, job.lease_expires_at):
            raise CommentGenerationJobConflict(job.id)
    changed = session.execute(
        update(GenerationJob)
        .where(
            GenerationJob.id == job.id,
            GenerationJob.job_version == int(job.job_version or 1),
        )
        .values(
            state="generating",
            generation_owner_id=owner,
            generation_lease_epoch=int(job.generation_lease_epoch or 0) + 1,
            lease_expires_at=now_value + GENERATION_LEASE,
            job_version=int(job.job_version or 1) + 1,
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    if changed != 1:
        raise CommentGenerationJobConflict(job.id)


def finish_comment_generation_job(
    session: Session,
    action: Action,
    payload,
    *,
    state: str,
    owner: str,
) -> None:
    """评论生成 attempt 终结：owner CAS 写 ready/failed/unknown，清空租约。

    job 丢失或 owner 不匹配时抛 typed 错误——评论生成结果不允许写到别人
    持有的审计行上；调用方按冲突处理，不静默跳过。
    """
    if state not in _FINISHABLE_STATES:
        raise ValueError(f"comment_generation_job_invalid_finish_state:{state}")
    obligation_id = comment_generation_obligation_id(action, payload)
    changed = session.execute(
        update(GenerationJob)
        .where(
            GenerationJob.obligation_type == COMMENT_GENERATION_OBLIGATION_TYPE,
            GenerationJob.obligation_id == obligation_id,
            GenerationJob.state == "generating",
            GenerationJob.generation_owner_id == owner,
        )
        .values(
            state=state,
            candidate_hash=str(action.candidate_hash or ""),
            evaluator_evidence=dict(
                (action.result or {}).get("evaluator_evidence") or {},
            ),
            generation_owner_id="",
            lease_expires_at=None,
            job_version=GenerationJob.job_version + 1,
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    if changed != 1:
        raise CommentGenerationJobConflict(obligation_id)


def defer_comment_generation_job(
    session: Session,
    action: Action,
    payload,
    *,
    owner: str,
    next_retry_at: datetime,
) -> None:
    obligation_id = comment_generation_obligation_id(action, payload)
    job = session.scalar(select(GenerationJob).where(
        GenerationJob.obligation_type == COMMENT_GENERATION_OBLIGATION_TYPE,
        GenerationJob.obligation_id == obligation_id,
        GenerationJob.state == "generating",
        GenerationJob.generation_owner_id == owner,
    ))
    if job is None:
        raise CommentGenerationJobConflict(obligation_id)
    defer_generation_job(
        job,
        stage="waiting_provider",
        next_retry_at=next_retry_at,
    )


def invalidate_comment_generation_jobs(
    session: Session,
    action: Action,
    payload,
    *,
    reason: str,
) -> None:
    obligation_id = comment_generation_obligation_id(action, payload)
    session.execute(
        update(GenerationJob)
        .where(
            GenerationJob.obligation_type == COMMENT_GENERATION_OBLIGATION_TYPE,
            GenerationJob.obligation_id == obligation_id,
            GenerationJob.state.in_(("pending", "generating", "unknown")),
        )
        .values(
            state="failed",
            generation_owner_id="",
            lease_expires_at=None,
            candidate_hash="",
            evaluator_evidence={"invalidation_reason": reason},
            job_version=GenerationJob.job_version + 1,
        )
        .execution_options(synchronize_session=False)
    )


def _context_version(payload) -> int:
    """上下文快照版本：规则版本 + 面具版本 + 引用目标构成评论生成输入指纹。

    SHA-256 折叠到 31 位非负整数（PRD §4.2 禁止语言内建 hash，进程间稳定；
    `generation_jobs.context_snapshot_version` 为 int32，禁止乘法线性组合）。
    """
    rule_version = int(getattr(payload, "resolved_rule_set_version_id", 0) or 0)
    mask_version = int(getattr(payload, "account_mask_version", 0) or 0)
    reply_target = int(getattr(payload, "reply_to_message_id", 0) or 0)
    digest = hashlib.sha256(
        json.dumps([rule_version, mask_version, reply_target], separators=(",", ":")).encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF or 1


def _context_hash(payload) -> str:
    snapshot = {
        "channel_message_id": getattr(payload, "channel_message_id", None),
        "message_id": getattr(payload, "message_id", None),
        "reply_to_message_id": getattr(payload, "reply_to_message_id", None),
        "resolved_rule_set_version_id": getattr(
            payload,
            "resolved_rule_set_version_id",
            None,
        ),
        "account_mask_snapshot_hash": getattr(
            payload,
            "account_mask_snapshot_hash",
            "",
        ),
    }
    canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "COMMENT_GENERATION_OBLIGATION_TYPE",
    "CommentGenerationJobConflict",
    "claim_comment_generation_job",
    "comment_generation_obligation_id",
    "defer_comment_generation_job",
    "finish_comment_generation_job",
    "invalidate_comment_generation_jobs",
]
