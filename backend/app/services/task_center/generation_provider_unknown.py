from types import SimpleNamespace

from sqlalchemy import select

from app.models import GenerationJob
from app.services._common import _now

from .ai_generation_claim_lifecycle import release_generation_claim
from .ai_generation_commit import commit_generation_action, load_generation_batch
from .ai_generation_job_finish import finish_owned_job
from .ai_generation_state import GenerationAttemptStale, mark_attempt_outcome


PROVIDER_UNKNOWN = "provider_result_unknown"


def persist_group_provider_unknown(session, request, *, detail: str) -> None:
    """Keep business ownership while releasing the local completed worker claim."""
    batch = load_generation_batch(session, request)
    with session.no_autoflush:
        for action, payload in batch:
            _finish_unknown_job(session, action, owner=request.claim_owner)
            data = payload.model_dump(mode="json")
            data["ai_generation_status"] = PROVIDER_UNKNOWN
            data["ai_generation_result_cache"] = {}
            mark_attempt_outcome(data, request.attempt_id, PROVIDER_UNKNOWN, timestamp=_now())
            release_generation_claim(action, data)
            action.result = {
                **dict(action.result or {}), "success": False, "error_code": PROVIDER_UNKNOWN,
                "error_message": detail, "generation_stage": PROVIDER_UNKNOWN,
                "generation_outcome": PROVIDER_UNKNOWN, "validation_stage": "ai_generation_provider",
            }
            commit_generation_action(session, request, action)


def _finish_unknown_job(session, action, *, owner: str) -> None:
    job_id = str((action.payload or {}).get("generation_job_id") or "")
    if not job_id:
        return
    job = session.scalar(select(GenerationJob).where(GenerationJob.id == job_id).with_for_update())
    expected = (action.tenant_id, action.task_id, action.task_lifecycle_epoch, action.obligation_type, action.obligation_id)
    actual = (job.tenant_id, job.task_id, job.task_lifecycle_epoch, job.obligation_type, job.obligation_id) if job else None
    if actual != expected:
        raise GenerationAttemptStale("provider_unknown_job_scope_mismatch")
    claim = SimpleNamespace(action_id=action.id, job_id=job.id, owner=owner,
                            job_version=job.job_version, generation_lease_epoch=job.generation_lease_epoch)
    finish_owned_job(session, claim, job=job, action=action, state="unknown", generation_stage=PROVIDER_UNKNOWN)
