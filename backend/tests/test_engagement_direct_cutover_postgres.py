import json

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Task, TaskAccountGroupBindingSetRevision
from app.services.task_center import service
from app.services.task_center.engagement_direct_cutover import activate_cutover, verify_retirement
from app.services.task_center.engagement_cutover_capacity import preview_cutover_capacity
from app.services.task_center.engagement_retirement_cleanup import cleanup_cutover_batch, require_cutover_cleanup
from tests.test_engagement_direct_cutover import OPERATION, _retire, _scope, _work
from tests.test_engagement_upgrade_postgres import upgrade_database


def test_postgres_retire_cleanup_activate_and_independent_readback(upgrade_database):
    Base.metadata.create_all(upgrade_database)
    with Session(upgrade_database) as session:
        tasks, paused, spec = _scope(session)
        actions, attempts, _ = _work(session, tasks[0])
        actions[0].status = "skipped"
        actions[0].result = {"error_code": "task_lifecycle_gateway_fenced"}
        session.commit()
        evidence = [(row.id, row.status, row.gateway_call_started_at, row.result_snapshot) for row in attempts]
        preview, receipt = _retire(session, spec)
        receipt = json.loads(json.dumps(receipt))
        cleaned = cleanup_cutover_batch(session, receipt, OPERATION)
        session.commit()
        assert cleaned["actions"] == 1 and not any(cleaned["remaining"].values())
        assert evidence == [(row.id, row.status, row.gateway_call_started_at, row.result_snapshot) for row in attempts]
        activated = activate_cutover(session, receipt, OPERATION,
            start_replacement=service.start_task_in_transaction, require_cleanup=require_cutover_cleanup)
        assert activated["activated"] == 1
        paused_id = paused.id
        session.commit()
    with Session(upgrade_database) as verification:
        verification.execute(text("SET TRANSACTION READ ONLY"))
        capacity = preview_cutover_capacity(verification, preview)
        assert capacity["tasks"][0]["stable_members"] == 2
        assert capacity["shared_class_budgets"][0]["member_union_count"] == 2
        old, new = verify_retirement(verification, receipt)
        assert old[0].status == "stopped" and new[0].status == "running"
        binding = verification.scalar(select(TaskAccountGroupBindingSetRevision).where(
            TaskAccountGroupBindingSetRevision.task_id == new[0].id,
            TaskAccountGroupBindingSetRevision.state == "active"))
        assert binding.task_lifecycle_epoch == new[0].task_lifecycle_epoch
        assert verification.get(Task, paused_id).status == "paused"
        require_cutover_cleanup(verification, receipt)
