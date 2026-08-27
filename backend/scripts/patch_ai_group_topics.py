"""Guarded preview/apply patch for the seven approved AI group topic libraries."""

from __future__ import annotations

import argparse
import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import AuditLog, Task, TgGroup


LEGACY_TOPIC_DIRECTIONS = {"", "日常讨论、活动答疑", "群聊日常活跃"}
DEFAULT_TOPIC_DIRECTION = "同城老客交流与避坑讨论"
APPROVED_GROUP_TOPICS: dict[int, list[dict[str, Any]]] = {
    5998: [
        {"title": "新开课老师水头与身材测评", "description": "打听近期新开课老师水头与身材真实度，防照骗", "weight": 1},
        {"title": "服务态度与催钟机车避坑", "description": "讨论哪些老师态度好不机车，哪些老师催钟严重避坑", "weight": 1},
        {"title": "工兵探路先锋战报交流", "description": "老哥出击交作业，反馈真实体验与环境隔音", "weight": 1},
        {"title": "老熟客打听常去老师动态与上岸", "description": "打听某位老师是否休假、换场子或上岸", "weight": 1},
    ],
    5363: [
        {"title": "新老师开课动态与水头打听", "description": "打听近期新上架老师水头真实度与服务细节", "weight": 1},
        {"title": "踩雷避坑与机车老师吐槽", "description": "交流近期踩雷经历，提醒老哥们避坑防翻车", "weight": 1},
        {"title": "老哥夜间出击交作业战报", "description": "分享出击真实体验、配合度与环境评价", "weight": 1},
        {"title": "常去老师上岸退圈与换场打听", "description": "打听老熟客常去的老师最新动态", "weight": 1},
    ],
    3848: [
        {"title": "豆包/小炤/脆脆鲨老师动态打听", "description": "打听群里热门老师开课、休假或上岸退圈动态", "weight": 1},
        {"title": "新茶探路先锋与战报交流", "description": "工兵老哥先出击，群友等战报交作业", "weight": 1},
        {"title": "身材颜值真实度与照骗辨析", "description": "根据照片辨析修图痕迹，讨论高挑身材与配合度", "weight": 1},
        {"title": "夜间出击组局与不机车靠谱推荐", "description": "老哥想出击求靠谱老师推荐", "weight": 1},
    ],
    2818: [
        {"title": "频道新照片身材与照骗辨析", "description": "讨论频道新发老师照片的腿长身材真实度与修图痕迹", "weight": 1},
        {"title": "新开课老师服务配合度打听", "description": "打听新老师态度是否机车、是否催钟", "weight": 1},
        {"title": "工兵探路出击交作业战报", "description": "老哥出击反馈真实体验与环境评价", "weight": 1},
        {"title": "靠谱老师推荐与避坑交流", "description": "老哥夜间出击寻找靠谱好老师", "weight": 1},
    ],
    2821: [
        {"title": "夜间出击组局打听与求推荐", "description": "今晚有无靠谱不机车的好老师开课，老哥想出击", "weight": 1},
        {"title": "新茶水头真实度与照骗避坑", "description": "讨论新开课老师水头足不足，防止踩雷", "weight": 1},
        {"title": "出击交作业真实体验交流", "description": "分享上课过程中的配合度与服务态度", "weight": 1},
        {"title": "热门老师排课与预约打听", "description": "打听热门老师档期，提前预约避免跑空", "weight": 1},
    ],
    5828: [
        {"title": "新开课老师测评与水头打听", "description": "打听新上架老师水头与身材曲线", "weight": 1},
        {"title": "工兵探路先锋与交作业战报", "description": "刚交完作业的老哥反馈服务态度与机车程度", "weight": 1},
        {"title": "环境隔音与公寓安全避坑", "description": "讨论老师公寓环境隔音好坏与避坑要点", "weight": 1},
        {"title": "常去老熟人老师动态打听", "description": "打听常去的老师是不是换场子或上岸了", "weight": 1},
    ],
    5996: [
        {"title": "公寓新老师测评与水头打听", "description": "打听新开课老师的配合度与水头真实度", "weight": 1},
        {"title": "出击体验反馈与环境隔音避坑", "description": "讨论公寓环境隔音与老师服务态度", "weight": 1},
        {"title": "老哥出击交作业战报交流", "description": "分享真实出击体验，评价水头与流程", "weight": 1},
        {"title": "双号老司机避坑交流与组局", "description": "两位老哥一问一答交流最新开课动态", "weight": 1},
    ],
}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("preview", "apply", "readback"), default="preview")
    parser.add_argument("--expected-fingerprint", default="")
    parser.add_argument("--expected-task-count", type=int)
    parser.add_argument("--expected-group-count", type=int)
    parser.add_argument("--requested-by", default="")
    parser.add_argument("--approved-by", default="")
    parser.add_argument("--approval-ref", default="")
    return parser.parse_args()


def _target_rows(session: Session, *, lock: bool) -> tuple[list[Task], list[TgGroup]]:
    group_ids = tuple(APPROVED_GROUP_TOPICS)
    task_query = select(Task).where(
        Task.type == "group_ai_chat",
        Task.status == "running",
        Task.deleted_at.is_(None),
        Task.type_config["target_group_id"].as_integer().in_(group_ids),
    )
    group_query = select(TgGroup).where(TgGroup.id.in_(group_ids))
    if lock:
        task_query = task_query.with_for_update()
        group_query = group_query.with_for_update()
    tasks = list(session.scalars(task_query).all())
    groups = list(session.scalars(group_query).all())
    return sorted(tasks, key=lambda item: item.id), sorted(groups, key=lambda item: item.id)


def _snapshot(tasks: list[Task], groups: list[TgGroup]) -> dict[str, Any]:
    task_rows = []
    for task in tasks:
        config = dict(task.type_config or {})
        group_id = int(config.get("target_group_id") or 0)
        task_rows.append({
            "id": task.id,
            "tenant_id": task.tenant_id,
            "config_revision": task.config_revision,
            "target_group_id": group_id,
            "old_topic_directions": config.get("topic_directions") or [],
            "new_topic_directions": APPROVED_GROUP_TOPICS[group_id],
            "old_adult_prompt_enabled": config.get("adult_prompt_enabled"),
            "new_adult_prompt_enabled": True,
        })
    group_rows = [{
        "id": group.id,
        "old_topic_direction": group.topic_direction or "",
        "new_topic_direction": DEFAULT_TOPIC_DIRECTION,
    } for group in groups]
    return {"tasks": task_rows, "groups": group_rows}


def _fingerprint(snapshot: dict[str, Any]) -> str:
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _validate_apply(args: argparse.Namespace, snapshot: dict[str, Any]) -> None:
    if not args.expected_fingerprint or args.expected_fingerprint != _fingerprint(snapshot):
        raise ValueError("expected_fingerprint_mismatch")
    if args.expected_task_count != len(snapshot["tasks"]):
        raise ValueError("expected_task_count_mismatch")
    if args.expected_group_count != len(snapshot["groups"]):
        raise ValueError("expected_group_count_mismatch")
    if not args.requested_by or not args.approved_by or not args.approval_ref:
        raise ValueError("requester_approver_and_reference_required")
    if args.requested_by == args.approved_by:
        raise ValueError("requester_and_approver_must_differ")


def _apply(
    session: Session,
    args: argparse.Namespace,
    *,
    tasks: list[Task],
    groups: list[TgGroup],
) -> None:
    snapshot = _snapshot(tasks, groups)
    _validate_apply(args, snapshot)
    for task in tasks:
        config = dict(task.type_config or {})
        group_id = int(config.get("target_group_id") or 0)
        config["topic_directions"] = APPROVED_GROUP_TOPICS[group_id]
        config["adult_prompt_enabled"] = True
        task.type_config = config
        task.config_revision = int(task.config_revision or 0) + 1
    for group in groups:
        if (group.topic_direction or "") in LEGACY_TOPIC_DIRECTIONS:
            group.topic_direction = DEFAULT_TOPIC_DIRECTION
    session.add(AuditLog(
        tenant_id=None,
        actor=args.requested_by[:100],
        action="AI活群话题库精确补丁",
        target_type="group_ai_chat_topic_manifest",
        target_id=_fingerprint(snapshot)[:80],
        detail=json.dumps({"approved_by": args.approved_by, "approval_ref": args.approval_ref}, ensure_ascii=False),
    ))
    session.commit()


def _readback(tasks: list[Task], groups: list[TgGroup]) -> dict[str, Any]:
    task_ok = all(
        (task.type_config or {}).get("topic_directions") == APPROVED_GROUP_TOPICS[int((task.type_config or {})["target_group_id"])]
        and (task.type_config or {}).get("adult_prompt_enabled") is True
        for task in tasks
    )
    group_ok = all(
        group.topic_direction == DEFAULT_TOPIC_DIRECTION
        or group.topic_direction not in LEGACY_TOPIC_DIRECTIONS
        for group in groups
    )
    return {"task_count": len(tasks), "group_count": len(groups), "tasks_ok": task_ok, "groups_ok": group_ok}


def main() -> None:
    args = _arguments()
    with SessionLocal() as session:
        tasks, groups = _target_rows(session, lock=args.mode == "apply")
        snapshot = _snapshot(tasks, groups)
        if args.mode == "preview":
            print(json.dumps({"fingerprint": _fingerprint(snapshot), **snapshot}, ensure_ascii=False, sort_keys=True))
            return
        if args.mode == "apply":
            _apply(session, args, tasks=tasks, groups=groups)
            tasks, groups = _target_rows(session, lock=False)
        print(json.dumps(_readback(tasks, groups), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
