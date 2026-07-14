from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from time import monotonic

from sqlalchemy import delete, func, select

from app.database import Base, SessionLocal, engine
from app.models import (
    AccountStatus,
    Action,
    ChannelMessage,
    OperationTarget,
    RuleSet,
    RuleSetVersion,
    SchedulingSetting,
    Task,
    Tenant,
    TgAccount,
)
from app.services.task_center.executors import channel_comment


TENANT_ID = 914_214
TASK_ID = "pg-channel-comment-planner"
RULE_SET_ID = 914_214
RULE_VERSION_ID = 914_215
SECOND_MESSAGE_ID = 914_314


def test_postgres_two_channel_comment_planners_do_not_duplicate_pending_blueprints(monkeypatch):
    Base.metadata.create_all(engine)
    _cleanup()
    start = _configure_concurrent_planners(monkeypatch)
    try:
        _seed_scope()
        created_counts, elapsed = _run_two_planners(start)
        actions = _task_actions()
        assert sorted(created_counts) == [0, 2]
        assert elapsed < 5
        assert len(actions) == 2
        assert len({action.action_dedupe_key for action in actions}) == 2
        assert all(action.status == "pending" for action in actions)
        assert all(action.payload["comment_text"] == "" for action in actions)
        assert all(action.payload["ai_generation_status"] == "pending" for action in actions)
    finally:
        _cleanup()


def test_postgres_two_planners_keep_lifetime_cap_across_two_messages(monkeypatch):
    Base.metadata.create_all(engine)
    _cleanup()
    start = _configure_concurrent_planners(monkeypatch)
    try:
        _seed_scope(
            message_ids=[TENANT_ID, SECOND_MESSAGE_ID],
            max_total_comments=1,
            target_comments_per_message=1,
        )
        created_counts, elapsed = _run_two_planners(start)
        actions = _task_actions()
        assert sorted(created_counts) == [0, 1]
        assert elapsed < 5
        assert len(actions) == 1
    finally:
        _cleanup()


def _configure_concurrent_planners(monkeypatch) -> Barrier:
    start = Barrier(2)
    planning_ready = Barrier(2)
    original_planning_accounts = channel_comment._planning_accounts

    def synchronized_planning_accounts(*args, **kwargs):
        accounts = original_planning_accounts(*args, **kwargs)
        planning_ready.wait(timeout=5)
        return accounts

    monkeypatch.setattr(channel_comment, "_planning_accounts", synchronized_planning_accounts)
    monkeypatch.setattr(channel_comment, "tenant_learning_profile_preview", lambda *_args: _profile_preview())
    monkeypatch.setattr(channel_comment, "audit_learning_profile_use", lambda *_args: None)
    return start


def _profile_preview() -> dict:
    return {
        "profile_scene": "channel_comment",
        "profile_version": 3,
        "profile_hit_summary": "偏好具体问题",
        "profile_unavailable_reason": "",
    }


def _run_two_planners(start: Barrier) -> tuple[list[int], float]:
    def run_planner(_worker_id: int) -> int:
        start.wait(timeout=5)
        with SessionLocal() as session:
            created = channel_comment.build_plan(session, session.get(Task, TASK_ID))
            session.commit()
            return created

    started_at = monotonic()
    with ThreadPoolExecutor(max_workers=2) as pool:
        created_counts = list(pool.map(run_planner, range(2)))
    return created_counts, monotonic() - started_at


def _task_actions() -> list[Action]:
    with SessionLocal() as session:
        return list(session.scalars(select(Action).where(Action.task_id == TASK_ID)))


def _seed_rule_scope(session) -> None:
    session.add(Tenant(id=TENANT_ID, name="PG 评论 Planner"))
    session.flush()
    session.add(
        RuleSet(
            id=RULE_SET_ID,
            tenant_id=TENANT_ID,
            name="PG 评论规则",
            status="active",
            task_types=["channel_comment"],
        )
    )
    session.flush()
    session.add(
        RuleSetVersion(
            id=RULE_VERSION_ID,
            tenant_id=TENANT_ID,
            rule_set_id=RULE_SET_ID,
            version=1,
            status="published",
            filters={},
            output_checks={},
            transforms={},
        )
    )


def _seed_channel_scope(session, message_ids: list[int]) -> None:
    session.add(
        OperationTarget(
            id=TENANT_ID,
            tenant_id=TENANT_ID,
            target_type="channel",
            tg_peer_id=f"-100{TENANT_ID}",
            title="PG 测试频道",
            can_send=True,
            auth_status="已授权运营",
        )
    )
    session.flush()
    for offset, message_id in enumerate(message_ids):
        session.add(
            ChannelMessage(
                id=message_id,
                tenant_id=TENANT_ID,
                channel_target_id=TENANT_ID,
                message_id=9001 + offset,
                content_preview=f"PG 频道消息 {offset + 1}",
                comment_available=True,
            )
        )


def _seed_accounts(session) -> None:
    for account_id in (TENANT_ID + 1, TENANT_ID + 2):
        session.add(
            TgAccount(
                id=account_id,
                tenant_id=TENANT_ID,
                display_name=f"账号 {account_id}",
                username=f"pg_comment_{account_id}",
                tg_first_name=f"评论号{account_id}",
                avatar_object_key=f"avatars/{account_id}.jpg",
                profile_sync_status="已同步",
                phone_masked=str(account_id),
                status=AccountStatus.ACTIVE.value,
                health_score=100,
                session_ciphertext=f"session-{account_id}",
            )
        )


def _postgres_task(
    *,
    message_ids: list[int],
    max_total_comments: int,
    target_comments_per_message: int,
) -> Task:
    return Task(
        id=TASK_ID,
        tenant_id=TENANT_ID,
        name="PG 评论 Planner",
        type="channel_comment",
        status="running",
        account_config={"selection_mode": "all", "max_concurrent": 2},
        pacing_config={
            "mode": "fixed",
            "max_actions_per_hour": 10,
            "interval_seconds_min": 0,
            "interval_seconds_max": 0,
            "jitter_percent": 0,
        },
        type_config={
            "target_channel_id": TENANT_ID,
            "message_scope": "specific",
            "message_ids": message_ids,
            "target_comments_per_message": target_comments_per_message,
            "comment_count_jitter": 0,
            "max_total_comments": max_total_comments,
            "max_total_comments_jitter": 0,
            "max_comments_per_account_per_hour": 500,
            "comment_mode": "comment",
            "rule_set_version_id": RULE_VERSION_ID,
        },
        stats={},
    )


def _seed_scope(
    *,
    message_ids: list[int] | None = None,
    max_total_comments: int = 10,
    target_comments_per_message: int = 2,
) -> None:
    resolved_message_ids = message_ids or [TENANT_ID]
    with SessionLocal() as session:
        _seed_rule_scope(session)
        _seed_channel_scope(session, resolved_message_ids)
        _seed_accounts(session)
        session.add(
            _postgres_task(
                message_ids=resolved_message_ids,
                max_total_comments=max_total_comments,
                target_comments_per_message=target_comments_per_message,
            )
        )
        session.commit()


def _cleanup() -> None:
    with SessionLocal() as session:
        session.execute(delete(Action).where(Action.task_id == TASK_ID))
        session.execute(delete(Task).where(Task.id == TASK_ID))
        session.execute(delete(ChannelMessage).where(ChannelMessage.tenant_id == TENANT_ID))
        session.execute(delete(OperationTarget).where(OperationTarget.tenant_id == TENANT_ID))
        session.execute(delete(RuleSetVersion).where(RuleSetVersion.tenant_id == TENANT_ID))
        session.execute(delete(RuleSet).where(RuleSet.tenant_id == TENANT_ID))
        session.execute(delete(TgAccount).where(TgAccount.tenant_id == TENANT_ID))
        session.execute(delete(SchedulingSetting).where(SchedulingSetting.tenant_id == TENANT_ID))
        remaining = session.scalar(select(func.count()).select_from(Action).where(Action.task_id == TASK_ID))
        assert remaining == 0
        session.execute(delete(Tenant).where(Tenant.id == TENANT_ID))
        session.commit()
