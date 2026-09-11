"""Indexed Action reference counts; only aggregate rows cross the database boundary."""
from sqlalchemy import text
from sqlalchemy.orm import Session

# Keep each indexed condition selective even when a page requests every material.
# The per-material aggregate keeps PostgreSQL from flattening this into a broad scan.
ACTION_REFERENCE_COUNTS = text("""
    SELECT requested.material_id, counts.reference_count
    FROM unnest(CAST(:material_ids AS text[])) AS requested(material_id)
    CROSS JOIN LATERAL (
        SELECT count(*) AS reference_count
        FROM actions
        WHERE tenant_id = :tenant_id
          AND (material_reference_ids_v1(payload) || material_reference_ids_v1(result))
              && ARRAY[requested.material_id]
    ) counts
    WHERE counts.reference_count > 0
""")


def action_material_reference_counts(
    session: Session, tenant_id: int, *, material_ids: set[int],
) -> dict[int, int]:
    if not material_ids:
        return {}
    rows = session.execute(ACTION_REFERENCE_COUNTS, {
        "tenant_id": tenant_id, "material_ids": [str(value) for value in sorted(material_ids)],
    })
    return {int(material_id): int(count) for material_id, count in rows}
