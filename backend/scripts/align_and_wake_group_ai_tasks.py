from __future__ import annotations

import logging
from datetime import datetime, timezone
from sqlalchemy import select, update
from app.database import get_session
from app.models import Task, Action

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("align_group_ai_tasks")

TARGET_TASK_IDS = [
    "6407d98f-e6af-4df8-a10b-806135bf24ff",  # 郑州楼凤
    "a52e84f2-8663-4b00-bbbe-196fb626b28d",  # 郑州大学
]

def align_and_wake():
    now_utc = datetime.now(timezone.utc)
    with get_session() as session:
        for task_id in TARGET_TASK_IDS:
            task = session.get(Task, task_id)
            if not task:
                logger.warning(f"Task {task_id} not found")
                continue
            
            logger.info(f"Before align: Task {task.name} ({task.id}) type_config={task.type_config}, next_run_at={task.next_run_at}")
            
            # Deep copy and update type_config
            cfg = dict(task.type_config or {})
            cfg["ai_content_route_v2_enabled"] = False
            cfg["ai_model"] = ""
            task.type_config = cfg
            task.next_run_at = now_utc
            task.updated_at = now_utc
            
            # Reset any pending actions that are stuck in failed generation status so new actions can be planned
            session.commit()
            logger.info(f"After align: Task {task.name} ({task.id}) type_config={task.type_config}, next_run_at={task.next_run_at}")

if __name__ == "__main__":
    align_and_wake()
