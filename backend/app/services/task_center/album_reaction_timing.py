"""Use the real Timeline admission in a savepoint before freezing child count."""
from .account_pacing_guard import AccountPacingDeadlineExceeded, reserve_account_pacing


def available_album_children(session, task, *, account_id, children, due_at, deadline_at):
    count = 0
    # No external calls: the preview creates only transactional scheduling rows.
    savepoint = session.begin_nested()
    try:
        for message in children:
            reserve_account_pacing(session, tenant_id=task.tenant_id, task_id=task.id,
                account_id=account_id, slot_key=f"like:{task.id}:{message.id}:{account_id}",
                due_at=due_at, deadline_at=deadline_at,
                engagement_contract_version="unified_engagement_v1", action_class="reaction")
            count += 1
    except AccountPacingDeadlineExceeded:
        return count
    finally:
        savepoint.rollback()
    return count
