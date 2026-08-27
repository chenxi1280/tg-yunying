from __future__ import annotations

import argparse
import json
import time

from sqlalchemy import select

from app.database import SessionLocal
from app.models import ChannelMessage, OperationTarget, Task
from app.services.task_center.ai_generator import generate_channel_comments


SAMPLE_MESSAGES = (
    "这款收纳盒宽 12cm，适合桌面小物。",
    "今日 BTC 突破关键阻力位，短线关注回调支撑。",
    "频道导航更新，新增科技、数码和交流板块。",
)


def evaluate_comments(session, tenant_id: int, limit: int = 5) -> None:
    cases = _evaluation_cases(session, tenant_id, limit)
    results = [_evaluate_case(session, tenant_id, case) for case in cases]
    print(json.dumps(results, ensure_ascii=False, indent=2))


def _evaluation_cases(session, tenant_id: int, limit: int) -> list[dict]:
    messages = session.scalars(
        select(ChannelMessage)
        .where(ChannelMessage.tenant_id == tenant_id)
        .order_by(ChannelMessage.id.desc())
        .limit(limit)
    ).all()
    if not messages:
        return [
            {"id": index, "content": content, "target_label": "测试频道", "config": {}}
            for index, content in enumerate(SAMPLE_MESSAGES[:limit], start=1)
        ]
    return [_message_case(session, message) for message in messages]


def _message_case(session, message: ChannelMessage) -> dict:
    target = session.get(OperationTarget, message.channel_target_id)
    task = session.scalar(
        select(Task)
        .where(
            Task.tenant_id == message.tenant_id,
            Task.type == "channel_comment",
            Task.type_config["target_channel_id"].as_integer() == message.channel_target_id,
        )
        .order_by(Task.created_at.desc())
    )
    return {
        "id": message.id,
        "content": message.content_preview or message.message_url,
        "target_label": target.title if target else "频道",
        "config": dict(task.type_config or {}) if task else {},
    }


def _evaluate_case(session, tenant_id: int, case: dict) -> dict:
    started_at = time.monotonic()
    try:
        contents, tokens = generate_channel_comments(
            session,
            tenant_id,
            case["config"],
            count=1,
            message_content=case["content"],
            target_label=case["target_label"],
        )
        result = {"comment": contents[0] if contents else "", "tokens": tokens}
    except Exception as exc:  # noqa: BLE001 - shadow report must expose provider/pipeline errors.
        result = {"error": f"{type(exc).__name__}:{exc}"}
    return {
        "case_id": case["id"],
        "snippet": case["content"][:80],
        "latency_s": round(time.monotonic() - started_at, 2),
        "production_pipeline": result,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the production channel-comment pipeline")
    parser.add_argument("--tenant-id", type=int, default=1)
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    with SessionLocal() as session:
        evaluate_comments(session, args.tenant_id, limit=args.limit)


if __name__ == "__main__":
    main()
