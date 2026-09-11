"""Indexed Action reference counts; only aggregate rows cross the database boundary."""
from sqlalchemy import text
from sqlalchemy.orm import Session

ACTION_REFERENCE_COUNTS = text("""
    SELECT referenced.material_id, count(*)
    FROM actions
    CROSS JOIN LATERAL (
        SELECT DISTINCT material_id
        FROM unnest(material_reference_ids_v1(payload) || material_reference_ids_v1(result)) material_id
        WHERE material_id = ANY(CAST(:material_ids AS text[]))
    ) referenced
    WHERE tenant_id = :tenant_id
      AND (material_reference_ids_v1(payload) || material_reference_ids_v1(result))
          && CAST(:material_ids AS text[])
    GROUP BY referenced.material_id
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
