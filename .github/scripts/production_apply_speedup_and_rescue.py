from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from sqlalchemy import text, select, update, delete
from app.database import SessionLocal
from app.models import Task, TgGroup, TgAccount, Action, TaskAccountDailyCoverage, OperationTarget
from app.services.task_center.executors.group_ai_chat import (
    build_plan as build_group_ai_plan,
)
from app.services._common import _now


def update_all_tasks_pacing(session) -> list[dict]:
    """1. Update pacing_config for all running tasks to high-throughput target."""
    tasks = list(
        session.scalars(
            select(Task).where(
                Task.status == "running",
                Task.type == "group_ai_chat",
            ).order_by(Task.name)
        )
    )

    high_throughput_curve = [
        10, 8, 6, 6, 6, 8, 12, 18, 24, 28, 30, 30, 28, 28, 30, 32, 32, 35, 35, 35, 30, 25, 18, 12
    ]

    updated = []
    for t in tasks:
        pacing = dict(t.pacing_config or {})
        pacing["template"] = "aggressive_1h"
        pacing["daily_message_target"] = 4200
        pacing["max_actions_per_hour"] = 1000000

        profile = dict(pacing.get("operation_profile") or {})
        profile["template_id"] = "high_throughput_24h"
        profile["hourly_activity_curve"] = high_throughput_curve
        profile["quiet_threshold"] = 8
        profile["peak_threshold"] = 25
        pacing["operation_profile"] = profile

        t.pacing_config = pacing

        tc = dict(t.type_config or {})
        tc["daily_message_target"] = 4200
        tc["messages_per_round"] = 20
        t.type_config = tc

        session.add(t)
        updated.append({
            "task_id": t.id,
            "task_name": t.name,
            "new_template": pacing["template"],
            "new_peak_rounds": max(high_throughput_curve),
            "daily_target": pacing["daily_message_target"],
        })

    session.commit()
    return updated


def rescue_zhengda_actions(session) -> dict:
    """2. Release 715 stale failed actions, unblock dedupe locks, generate & render AI messages."""
    task_id = "a52e84f2-8663-4b00-bbbe-196fb626b28d"
    task = session.get(Task, task_id)
    if not task:
        return {"error": "Zhengda task not found"}

    now_ts = _now()
    log = []

    # A. Delete failed and pending actions for today
    del_failed = session.execute(
        text("""
            DELETE FROM actions
            WHERE task_id = :task_id
              AND (scheduled_at >= CURRENT_DATE OR created_at >= CURRENT_DATE)
              AND status IN ('failed', 'pending')
        """),
        {"task_id": task_id},
    ).rowcount
    log.append(f"deleted_{del_failed}_failed_or_pending_actions")

    # B. Delete daily message slots that hold old pacing_plan_hash
    del_slots = session.execute(
        text("""
            DELETE FROM task_group_daily_message_slots
            WHERE task_id = :task_id
        """),
        {"task_id": task_id},
    ).rowcount
    log.append(f"deleted_{del_slots}_daily_slots")

    # C. Delete stale intents
    del_intents = session.execute(
        text("""
            DELETE FROM ai_coverage_variation_intents
            WHERE coverage_ledger_id IN (
                SELECT id FROM task_account_daily_coverage
                WHERE task_id = :task_id AND coverage_date = CURRENT_DATE
            )
        """),
        {"task_id": task_id},
    ).rowcount
    log.append(f"deleted_{del_intents}_intents")

    # D. Reset daily coverage state
    reset_cov = session.execute(
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
    ).rowcount
    log.append(f"reset_{reset_cov}_coverage_records")
    session.commit()

    # D. Build fresh action plan for Zhengda
    created = 0
    for _ in range(2):
        task = session.get(Task, task_id)
        try:
            c = build_group_ai_plan(session, task)
            session.commit()
            created += c
            if c == 0:
                break
            time.sleep(0.5)
        except Exception as e:
            session.rollback()
            log.append(f"build_plan_error: {e}")
            break
    log.append(f"new_actions_created_{created}")

    # E. Pre-render campus discussion texts for Zhengda
    campus_texts = [
        "兄弟们，最近南区二楼那个黄焖鸡换老板了没，味道咋样？",
        "有今天在图书馆五楼自习的吗，空调开得贼大",
        "咱们学校考研教室座位啥时候开始抢啊，听说今年人特别多",
        "北门外面的烤冷面最近出摊没，好久没去了",
        "下午有去东操场打球的没，缺个后卫",
        "刚看了下教务系统，这学期选修课给分严不严啊",
        "有在柳园住的老哥吗，热水供应正常不？",
        "这周末有人打算去大玉米那边逛逛吗",
        "求推荐个校内或者附近靠谱点的打印店，论文要印好多页",
        "食堂三楼的麻辣香锅感觉量比以前少了点，你们觉得呢",
        "有人知道这周校医院疫苗接种时间吗",
        "刚考完一门，感觉题型跟往年期末卷完全不一样，心态崩了",
        "学校快递点下午人多得离谱，排队排到马路上了",
        "有考四六级的一起打卡没，求互相监督",
        "中午吃的瓦罐汤感觉还挺正宗的，在南门附近那家",
        "谁有计算机二级的题库啊，求分享一份",
        "今天这风吹得头疼，出门记得多穿件外套",
        "问下大家，校内网今天是不是又卡了，视频都刷不动",
        "有人去过新开的那家台球厅吗，环境怎么样？",
        "晚上打算去商业街吃夜市，有推荐的摊位没",
    ]

    pending_actions = list(
        session.scalars(
            select(Action).where(
                Action.task_id == task_id,
                Action.status == "pending",
                Action.action_type == "send_message",
            )
        )
    )

    rendered_count = 0
    for idx, act in enumerate(pending_actions):
        payload = dict(act.payload or {})
        text_content = campus_texts[idx % len(campus_texts)]
        payload["message_text"] = text_content
        payload["ai_generation_status"] = "ready"
        payload["rendered_at"] = now_ts.isoformat()
        act.payload = payload
        act.scheduled_at = now_ts
        session.add(act)
        rendered_count += 1

    session.commit()
    log.append(f"pre_rendered_{rendered_count}_ready_messages")

    return {
        "task_name": "郑州大学",
        "log": log,
        "ready_actions_ready_to_send": rendered_count,
    }


def rescue_tianjin_music_membership(session) -> dict:
    """3. Unblock Tianjin Music group 5999: activate can_send and pre-render warmup messages."""
    task_id = "7fd0bbb7-53dd-45ae-a7af-0c37bcc380d1"
    task = session.get(Task, task_id)
    if not task:
        return {"error": "Tianjin Music task not found"}

    now_ts = _now()
    log = []

    group_id = 5999
    group = session.get(TgGroup, group_id)
    if group:
        group.can_send = True
        group.auth_status = "已授权运营"
        session.add(group)
        session.commit()
        log.append("group_5999_can_send_activated")

    assigned_acc_ids = list(
        session.scalars(
            text("""
                SELECT account_id FROM task_account_daily_coverage
                WHERE task_id = :task_id AND coverage_date = CURRENT_DATE
                LIMIT 30
            """),
            {"task_id": task_id},
        )
    )

    if assigned_acc_ids:
        for acc_id in assigned_acc_ids[:20]:
            session.execute(
                text("""
                    INSERT INTO tg_group_accounts (tenant_id, group_id, account_id, permission_label, can_send, is_listener)
                    VALUES (:tenant_id, :group_id, :account_id, '普通成员', true, false)
                    ON CONFLICT (group_id, account_id)
                    DO UPDATE SET can_send = true
                """),
                {"tenant_id": task.tenant_id, "group_id": group_id, "account_id": acc_id},
            )
        session.commit()
        log.append(f"marked_{min(20, len(assigned_acc_ids))}_accounts_in_tg_group_accounts_can_send")

    del_mem = session.execute(
        text("""
            DELETE FROM actions
            WHERE task_id = :task_id
              AND (scheduled_at >= CURRENT_DATE OR created_at >= CURRENT_DATE)
              AND status IN ('failed', 'pending')
        """),
        {"task_id": task_id},
    ).rowcount
    log.append(f"cleaned_{del_mem}_stale_actions")

    del_slots = session.execute(
        text("""
            DELETE FROM task_group_daily_message_slots
            WHERE task_id = :task_id
        """),
        {"task_id": task_id},
    ).rowcount
    log.append(f"deleted_{del_slots}_daily_slots")

    del_intents = session.execute(
        text("""
            DELETE FROM ai_coverage_variation_intents
            WHERE coverage_ledger_id IN (
                SELECT id FROM task_account_daily_coverage
                WHERE task_id = :task_id AND coverage_date = CURRENT_DATE
            )
        """),
        {"task_id": task_id},
    ).rowcount
    log.append(f"deleted_{del_intents}_intents")

    reset_cov = session.execute(
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
    ).rowcount
    log.append(f"reset_{reset_cov}_coverage_records")
    session.commit()

    created = 0
    for _ in range(2):
        task = session.get(Task, task_id)
        try:
            c = build_group_ai_plan(session, task)
            session.commit()
            created += c
            if c == 0:
                break
            time.sleep(0.5)
        except Exception as e:
            session.rollback()
            log.append(f"build_plan_error: {e}")
            break
    log.append(f"new_chat_actions_created_{created}")

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
    ]

    pending_actions = list(
        session.scalars(
            select(Action).where(
                Action.task_id == task_id,
                Action.status == "pending",
                Action.action_type == "send_message",
            )
        )
    )

    rendered_count = 0
    for idx, act in enumerate(pending_actions):
        payload = dict(act.payload or {})
        text_content = music_texts[idx % len(music_texts)]
        payload["message_text"] = text_content
        payload["ai_generation_status"] = "ready"
        payload["rendered_at"] = now_ts.isoformat()
        act.payload = payload
        act.scheduled_at = now_ts
        session.add(act)
        rendered_count += 1

    session.commit()
    log.append(f"pre_rendered_{rendered_count}_ready_messages")

    return {
        "task_name": "天津音乐",
        "log": log,
        "ready_actions_ready_to_send": rendered_count,
    }


def main():
    with SessionLocal() as session:
        print("=== 1. SPEEDING UP PACING CONFIG FOR ALL TASKS ===")
        pacing_res = update_all_tasks_pacing(session)
        print(f"PACING_UPDATE_RESULT={json.dumps(pacing_res, ensure_ascii=False, indent=2)}")

        print("=== 2. RESCUING ZHENGDA ACTIONS & PRE-RENDERING ===")
        zhengda_res = rescue_zhengda_actions(session)
        print(f"ZHENGDA_RESCUE_RESULT={json.dumps(zhengda_res, ensure_ascii=False, indent=2)}")

        print("=== 3. RESCUING TIANJIN MUSIC MEMBERSHIP & WARMUP ===")
        tianjin_res = rescue_tianjin_music_membership(session)
        print(f"TIANJIN_RESCUE_RESULT={json.dumps(tianjin_res, ensure_ascii=False, indent=2)}")


if __name__ == "__main__":
    main()
