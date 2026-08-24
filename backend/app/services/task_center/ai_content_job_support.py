from __future__ import annotations

import hashlib
import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, GenerationJob

from .ai_content_job_binding_error import AiContentJobBindingError


def generation_jobs_for_batch(
    session: Session,
    batch: list[tuple[Action, object]],
) -> tuple[GenerationJob, ...]:
    job_ids = tuple(
        str(getattr(payload, "generation_job_id", "") or "")
        for _action, payload in batch
    )
    if not all(job_ids) or len(set(job_ids)) != len(job_ids):
        raise AiContentJobBindingError("generation_job_batch_invalid")
    jobs = session.scalars(select(GenerationJob).where(GenerationJob.id.in_(job_ids))).all()
    by_id = {job.id: job for job in jobs}
    if len(by_id) != len(job_ids):
        raise AiContentJobBindingError("generation_job_missing_for_content_binding")
    return tuple(by_id[job_id] for job_id in job_ids)


def due_at(action: Action) -> datetime:
    return action.pacing_due_at or action.release_not_before_at or action.scheduled_at


def registry_version(registry: dict, route: str, label: str) -> str:
    value = dict(registry or {}).get(route)
    version = str(value.get("version") if isinstance(value, dict) else value or "").strip()
    if not version:
        raise AiContentJobBindingError(f"{label}_version_missing:{route}")
    return version


def example_version(example_set: dict) -> str:
    version = str(dict(example_set or {}).get("version") or "").strip()
    if not version:
        raise AiContentJobBindingError("example_set_version_missing")
    return version


def stable_hash(value: dict) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "due_at",
    "example_version",
    "generation_jobs_for_batch",
    "registry_version",
    "stable_hash",
]
