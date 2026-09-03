from __future__ import annotations

import json
from sqlalchemy import text, select, update
from app.database import SessionLocal
from app.models import Task, Action, TaskAccountDailyCoverage
from app.services.task_center.executors.group_ai_chat import build_plan as build_group_ai_plan
from app.services._common import _now


def setup_independent_warmup_actions(session, task_id: str, sample_texts: list[str], count: int = 5):
    task = session.get(Task, task_id)
    if not task:
        return {"error": f"task {task_id} not found"}

    now_ts = _now()

    # 1. Clean stale actions and slots
    session.execute(
        text("""
            DELETE FROM actions
            WHERE task_id = :task_id
              AND (scheduled_at >= CURRENT_DATE OR created_at >= CURRENT_DATE)
              AND status IN ('failed', 'skipped', 'pending')
        """),
        {"task_id": task_id},
    )
    session.execute(
        text("DELETE FROM task_group_daily_message_slots WHERE task_id = :task_id"),
        {"task_id": task_id},
    )
    session.execute(
        text("""
            DELETE FROM ai_coverage_variation_intents
            WHERE coverage_ledger_id IN (
                SELECT id FROM task_account_daily_coverage
                WHERE task_id = :task_id AND coverage_date = CURRENT_DATE
            )
        """),
        {"task_id": task_id},
    )
    session.execute(
        update(TaskAccountDailyCoverage)
        .where(
            TaskAccountDailyCoverage.task_id == task_id,
            TaskAccountDailyCoverage.coverage_date == now_ts.date(),
        )
        .values(
            state="ready",
            reserved_action_id=None,
            reservation_token=None,
            blocker_code="",
            updated_at=now_ts,
        )
    )
    session.commit()

    # 2. Build fresh plan
    build_group_ai_plan(session, task)
    session.commit()

    # 3. Fetch newly built actions and make them completely independent
    actions = list(
        session.scalars(
            select(Action).where(
                Action.task_id == task_id,
                Action.status == "pending",
                Action.action_type == "send_message",
            ).order_by(Action.created_at.asc()).limit(count)
        )
    )

    for i, act in enumerate(actions):
        payload = dict(act.payload or {})
        payload["message_text"] = sample_texts[i % len(sample_texts)]
        payload["ai_generation_status"] = "ready"
        payload["rendered_at"] = now_ts.isoformat()
        payload["reply_to_message_id"] = None
        payload["context_message_ids"] = []
        payload["anchor_message_ids"] = []
        payload["context_snapshot_message_id"] = None
        payload["chat_mode"] = "broadcast"
        payload["ai_message_memory_id"] = None
        act.payload = payload
        act.scheduled_at = now_ts
        session.add(act)

    session.commit()
    return {"task_id": task_id, "task_name": task.name, "independent_ready_count": len(actions)}


def main():
    with SessionLocal() as session:
        # 郑州大学
        zhengda_texts = [
            "兄弟们，最近南区二楼那个黄焖鸡换老板了没，味道咋样？",
            "有今天在图书馆五楼自习的吗，空调开得贼大",
            "咱们学校考研教室座位啥时候开始抢啊，听说今年人特别多",
            "北门外面的烤冷面最近出摊没，好久没去了",
            "这周天气真热，下午有去游泳馆泡着的没"
        ]
        res_zd = setup_independent_warmup_actions(
            session, "a52e84f2-8663-4b00-bbbe-196fb626b28d", zhengda_texts, count=5
        )

        # 天津音乐
        tianjin_texts = [
            "兄弟们，最近这新场子质量咋样，有人去踩过点没？",
            "刚进群，冒个泡，有老哥分享下经验不",
            "最近有推荐的老师吗，服务靠谱点的",
            "群里老哥都在不，下午有开课的没",
            "看着榜单还行，不知道真人照骗多不多 🤔"
        ]
        res_tj = setup_independent_warmup_actions(
            session, "7fd0bbb7-53dd-45ae-a7af-0c37bcc380d1", tianjin_texts, count=5
        )

        print(f"FIRE_INDEPENDENT_WARMUP_RESULT={json.dumps([res_zd, res_tj], ensure_ascii=False, indent=2)}")


if __name__ == "__main__":
    main()
