"""Share identical evidence without caching mutable admission decisions."""
import hashlib
import json

from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.models import PlanningAdmissionEvidence


def canonical_paths(paths: list[dict]) -> str:
    return json.dumps(paths, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def admission_paths_digest(paths: list[dict]) -> str:
    return hashlib.sha256(canonical_paths(paths).encode()).hexdigest()


def shared_admission_evidence(session, tenant_id: int, paths: list[dict]) -> PlanningAdmissionEvidence:
    canonical = canonical_paths(paths)
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    existing = session.get(PlanningAdmissionEvidence, (tenant_id, digest))
    if existing is not None:
        return existing
    dialect = session.get_bind().dialect.name
    insert = {"postgresql": postgres_insert, "sqlite": sqlite_insert}[dialect]
    # Concurrent planners share the unique content identity in this transaction.
    session.execute(insert(PlanningAdmissionEvidence).values(
        tenant_id=tenant_id, digest=digest, account_paths=json.loads(canonical),
    ).on_conflict_do_nothing(index_elements=["tenant_id", "digest"]))
    return session.get(PlanningAdmissionEvidence, (tenant_id, digest))
