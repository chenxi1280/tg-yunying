from sqlalchemy import event

from app.database import SessionLocal
from app.models import AiContentPolicyVersion, Task, TaskAiContentPolicyBinding
from app.services.task_center.ai_generation_policy_ownership import generation_policy_snapshots
from tests.test_ai_content_window_retirement_postgres import _seed


def test_postgres_reads_original_policy_without_mutating_unknown_generation():
    with SessionLocal() as session:
        action, job, slot = _seed(session)
        task = session.get(Task, action.task_id)
        task.config_revision = 2
        job.state = "unknown"
        job.window_plan_hash = "h" * 64
        job.content_policy_hash = "c" * 64
        job.task_binding_hash = job.task_direction_snapshot_hash = "b" * 64
        policy = AiContentPolicyVersion(
            id="ownership-policy-pg", tenant_id=task.tenant_id, version=1,
            status="retired", policy_hash="c" * 64,
        )
        session.add(policy)
        session.flush()
        binding = TaskAiContentPolicyBinding(
            tenant_id=task.tenant_id, task_id=task.id, task_lifecycle_epoch=1,
            task_config_revision=1, policy_version_id=policy.id,
            allowed_routes=["general"], evidence_hash="b" * 64,
        )
        session.add(binding)
        session.flush()
        statements = []

        def record(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement.split()[0].upper())

        connection = session.connection()
        event.listen(connection, "before_cursor_execute", record)
        try:
            snapshot = generation_policy_snapshots(session, task, (job,))[job.id]
        finally:
            event.remove(connection, "before_cursor_execute", record)

        assert snapshot.binding.task_config_revision == 1
        assert snapshot.policy.id == policy.id
        assert snapshot.frozen_route == "general"
        assert statements == ["SELECT", "SELECT"]
        assert job.state == "unknown" and slot.state == "gateway_bound"
        assert job.window_slot_id == slot.id and task.config_revision == 2
        session.rollback()
