from __future__ import annotations

import json
import time
from sqlalchemy import text, select, update
from app.database import SessionLocal
from app.models import Task, TgGroup, Action, TaskAccountDailyCoverage
from app.services.task_center.executors.group_ai_chat import build_plan as build_group_ai_plan
from app.services._common import _now


def main():
    task_id = "7fd0bbb7-53dd-45ae-a7af-0c37bcc380d1"
    with SessionLocal() as session:
        task = session.get(Task, task_id)
        if not task:
            print("Tianjin task not found")
            return

        now_ts = _now()
        group_id = 5999

        # 1. Clean actions and slots
        session.execute(
            text("""
                DELETE FROM actions
                WHERE task_id = :task_id
                  AND (scheduled_at >= CURRENT_DATE OR created_at >= CURRENT_DATE)
                  AND status IN ('failed', 'pending')
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

        # 2. Reset coverage state
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

        # 3. Build 1 round of chat actions
        c = build_group_ai_plan(session, task)
        session.commit()
        print(f"Created {c} chat actions for Tianjin")

        # 4. Pre-render warmup messages
        music_texts = [
            "兄弟们，最近这新场子质量咋样，有人去踩过点没？",
            "刚进群，冒个泡，有老哥分享下经验不",
            "最近有推荐的老师吗，服务靠谱点的",
            "群里老哥都在不，下午有开课的没",
            "看着榜单还行，不知道真人照骗多不多 🤔",
            "有去过大悦城附近那家的吗，环境怎么样？",
            "价格还算公道，就怕现场降档次 😂",
            "刚吃完饭歇着，兄弟们今天有啥好节目",
            "有体验过真独龙的老哥出来说说感受呗",
            "签到打卡，等一个真实探店反馈",
            "有人去过河东那家吗，求靠谱推荐",
            "下午闲着没事，群里老哥出来唠唠嗑呗",
            "这天气太热了，下午适合在店里吹空调",
            "有推荐的兼职老师吗，别太坑就行",
            "今天群里挺安静啊，大家都在潜水呢"
        ]

        pending = list(
            session.scalars(
                select(Action).where(
                    Action.task_id == task_id,
                    Action.status == "pending",
                    Action.action_type == "send_message",
                )
            )
        )

        for i, act in enumerate(pending):
            payload = dict(act.payload or {})
            payload["message_text"] = music_texts[i % len(music_texts)]
            payload["ai_generation_status"] = "ready"
            payload["rendered_at"] = now_ts.isoformat()
            act.payload = payload
            act.scheduled_at = now_ts
            session.add(act)

        session.commit()
        print(f"Pre-rendered {len(pending)} ready actions for Tianjin Music")


if __name__ == "__main__":
    main()
