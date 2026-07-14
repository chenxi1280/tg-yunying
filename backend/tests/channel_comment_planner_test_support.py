import ast
import inspect

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountStatus,
    Action,
    ChannelMessage,
    ChannelMessageComment,
    OperationTarget,
    Task,
    Tenant,
    TgAccount,
)
from app.services import group_listeners
from app.services.grok_cli_bridge import GrokCliBridge
from app.services.task_center import ai_generator
from app.services.task_center.executors import channel_comment
from app.services.task_center.executors import common as executor_common


def planner_session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed_comment_channel(session: Session) -> None:
    session.add(Tenant(id=1, name="测试租户"))
    session.add(
        OperationTarget(
            id=31,
            tenant_id=1,
            target_type="channel",
            tg_peer_id="-10031",
            title="测试频道",
            can_send=True,
            auth_status="已授权运营",
        )
    )
    session.add(
        ChannelMessage(
            id=41,
            tenant_id=1,
            channel_target_id=31,
            message_id=9001,
            content_preview="频道消息正文",
            comment_available=True,
        )
    )
    for index, reply_id in enumerate((8101, 8102), 1):
        session.add(
            ChannelMessageComment(
                tenant_id=1,
                channel_target_id=31,
                channel_message_id=41,
                comment_message_id=reply_id,
                author_name=f"读者 {index}",
                content_preview=f"读者评论 {index}",
            )
        )


def _seed_comment_accounts(session: Session) -> None:
    for account_id in (101, 102, 103):
        session.add(
            TgAccount(
                id=account_id,
                tenant_id=1,
                display_name=f"账号 {account_id}",
                username=f"comment_{account_id}",
                tg_first_name=f"评论号{account_id}",
                avatar_object_key=f"avatars/{account_id}.jpg",
                profile_sync_status="已同步",
                phone_masked=str(account_id),
                status=AccountStatus.ACTIVE.value,
                health_score=100,
                session_ciphertext=f"session-{account_id}",
            )
        )


def _comment_task_config(
    mode: str,
    *,
    reply_min: int,
    requested_reply_ids: list[int] | None,
    target_count: int,
) -> dict:
    return {
        "target_channel_id": 31,
        "message_scope": "specific",
        "message_ids": [41],
        "target_comments_per_message": target_count,
        "comment_count_jitter": 0,
        "max_total_comments": 20,
        "max_total_comments_jitter": 0,
        "max_comments_per_account_per_hour": 500,
        "comment_mode": mode,
        "reply_min_per_message": reply_min,
        "reply_to_message_ids": requested_reply_ids or [],
    }


def _comment_task(
    mode: str,
    *,
    reply_min: int,
    requested_reply_ids: list[int] | None,
    target_count: int,
) -> Task:
    return Task(
        id=f"comment-phase-a-{mode}",
        tenant_id=1,
        name=f"{mode} 评论",
        type="channel_comment",
        status="running",
        account_config={
            "selection_mode": "all",
            "max_concurrent": 3,
            "cooldown_per_account_minutes": 0,
        },
        pacing_config={
            "mode": "fixed",
            "max_actions_per_hour": 10,
            "interval_seconds_min": 0,
            "interval_seconds_max": 0,
            "jitter_percent": 0,
        },
        type_config=_comment_task_config(
            mode,
            reply_min=reply_min,
            requested_reply_ids=requested_reply_ids,
            target_count=target_count,
        ),
        stats={},
    )


def seed_comment_task(
    session: Session,
    *,
    mode: str,
    reply_min: int = 0,
    requested_reply_ids: list[int] | None = None,
    target_count: int = 2,
) -> Task:
    _seed_comment_channel(session)
    _seed_comment_accounts(session)
    task = _comment_task(
        mode,
        reply_min=reply_min,
        requested_reply_ids=requested_reply_ids,
        target_count=target_count,
    )
    session.add(task)
    session.commit()
    return task


def forbid_planner_external_boundaries(monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        pytest.fail("channel comment Planner must not call an external boundary")

    monkeypatch.setattr(ai_generator, "generate_channel_comments", fail)
    monkeypatch.setattr(ai_generator, "generate_channel_reply_comments", fail)
    monkeypatch.setattr(ai_generator, "generate_contents", fail)
    monkeypatch.setattr(ai_generator.ai_gateway, "generate_drafts", fail)
    monkeypatch.setattr(GrokCliBridge, "generate", fail)
    monkeypatch.setattr(executor_common, "collect_channel_messages", fail)
    monkeypatch.setattr(executor_common.gateway, "fetch_channel_messages", fail)
    monkeypatch.setattr(group_listeners, "collect_group_context", fail)
    monkeypatch.setattr(group_listeners.gateway, "fetch_group_messages", fail)


def add_existing_comment_action(session: Session, task: Task, status: str) -> None:
    session.add(
        Action(
            id=f"existing-{status}",
            tenant_id=task.tenant_id,
            task_id=task.id,
            task_type="channel_comment",
            action_type="post_comment",
            account_id=101,
            status=status,
            payload={
                "channel_target_id": 31,
                "channel_message_id": 41,
                "message_id": 9001,
                "slot_id": "channel-comment:41:0",
                "comment_text": "existing",
            },
        )
    )
    session.commit()


def planner_external_boundary_references() -> set[str]:
    tree = ast.parse(inspect.getsource(channel_comment))
    references: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            references.add(str(node.module or ""))
            references.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            references.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                references.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                references.add(node.func.attr)
    return references


def fixed_profile(monkeypatch) -> None:
    monkeypatch.setattr(
        channel_comment,
        "tenant_learning_profile_preview",
        lambda *_args: {
            "profile_scene": "channel_comment",
            "profile_version": 7,
            "profile_hit_summary": "读者偏好追问具体细节",
            "profile_unavailable_reason": "",
        },
    )
    monkeypatch.setattr(channel_comment, "audit_learning_profile_use", lambda *_args: None)
