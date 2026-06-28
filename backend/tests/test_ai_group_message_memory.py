from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.models import Action, AiGroupMessageMemory
from app.services._common import _now
from app.services.task_center.ai_message_memory import (
    DuplicateMessageReservation,
    ensure_group_ai_message_sendable,
    expire_stale_group_ai_reservations,
    mark_group_ai_message_result,
    normalize_group_ai_text,
    reserve_group_ai_message,
    backfill_group_ai_message_memory_from_actions,
)
from app.services.task_center.ai_message_memory_maintenance import drain_ai_message_memory_maintenance


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def test_normalize_group_ai_text_ignores_cosmetic_variation():
    assert normalize_group_ai_text(" 花花老师  身材真好！！！😊😊 ") == normalize_group_ai_text("花花老师身材真好!")


def test_reserved_message_blocks_same_fingerprint_inside_five_minutes():
    now = _now()
    with _session() as session:
        first = reserve_group_ai_message(
            session,
            tenant_id=1,
            group_id=22,
            task_id="task-1",
            account_id=101,
            raw_text="花花老师身材真好",
            now=now,
        )
        session.commit()

        with pytest.raises(DuplicateMessageReservation) as exc:
            reserve_group_ai_message(
                session,
                tenant_id=1,
                group_id=22,
                task_id="task-1",
                account_id=102,
                raw_text=" 花花老师 身材真好！！！",
                now=now + timedelta(minutes=1),
            )

        assert exc.value.reference_id == first.id
        assert exc.value.duplicate_window == "5m_exact"


def test_unknown_after_send_and_success_participate_in_duplicate_check():
    now = _now()
    with _session() as session:
        unknown = reserve_group_ai_message(
            session,
            tenant_id=1,
            group_id=22,
            task_id="task-1",
            account_id=101,
            raw_text="主任最近约新妹子了",
            now=now,
        )
        mark_group_ai_message_result(session, unknown.id, status="unknown_after_send", action_id="action-1")
        success = reserve_group_ai_message(
            session,
            tenant_id=1,
            group_id=22,
            task_id="task-1",
            account_id=102,
            raw_text="鸡排哥的药",
            now=now,
        )
        mark_group_ai_message_result(session, success.id, status="success", action_id="action-2")
        session.commit()

        for text in ["主任最近约新妹子了", "鸡排哥的药"]:
            with pytest.raises(DuplicateMessageReservation):
                reserve_group_ai_message(
                    session,
                    tenant_id=1,
                    group_id=22,
                    task_id="task-1",
                    account_id=103,
                    raw_text=text,
                    now=now + timedelta(minutes=2),
                )


def test_expire_stale_reservations_marks_visible_status_without_deleting():
    now = _now()
    with _session() as session:
        memory = reserve_group_ai_message(
            session,
            tenant_id=1,
            group_id=22,
            task_id="task-1",
            account_id=101,
            raw_text="精品榜的妹子真好",
            now=now,
            reservation_ttl=timedelta(minutes=2),
        )
        session.commit()

        assert expire_stale_group_ai_reservations(session, now=now + timedelta(minutes=3)) == 1
        session.commit()

        rows = list(session.scalars(select(AiGroupMessageMemory)))
        assert len(rows) == 1
        assert rows[0].id == memory.id
        assert rows[0].status == "expired_before_send"
        assert rows[0].quality_decision == "expired_visible"


def test_reservation_key_is_database_unique_for_atomic_planner_races():
    now = _now()
    with _session() as session:
        first = AiGroupMessageMemory(
            tenant_id=1,
            group_id=22,
            task_id="task-1",
            account_id=101,
            raw_text="花花老师身材真好",
            normalized_text="花花老师身材真好",
            text_fingerprint="same-fingerprint",
            reservation_key="1:22:same-fingerprint:bucket",
            status="reserved",
            planned_at=now,
        )
        second = AiGroupMessageMemory(
            tenant_id=1,
            group_id=22,
            task_id="task-1",
            account_id=102,
            raw_text="花花老师身材真好",
            normalized_text="花花老师身材真好",
            text_fingerprint="same-fingerprint",
            reservation_key="1:22:same-fingerprint:bucket",
            status="reserved",
            planned_at=now,
        )
        session.add_all([first, second])

        with pytest.raises(IntegrityError):
            session.flush()


def test_one_hour_high_similarity_duplicate_blocks_rephrased_message():
    now = _now()
    with _session() as session:
        first = reserve_group_ai_message(
            session,
            tenant_id=1,
            group_id=22,
            task_id="task-1",
            account_id=101,
            raw_text="花花老师身材服务真好",
            now=now,
        )
        mark_group_ai_message_result(session, first.id, status="success", action_id="action-1")
        session.commit()

        with pytest.raises(DuplicateMessageReservation) as exc:
            reserve_group_ai_message(
                session,
                tenant_id=1,
                group_id=22,
                task_id="task-1",
                account_id=102,
                raw_text="花花老师服务身材挺好",
                now=now + timedelta(minutes=40),
            )

        assert exc.value.reference_id == first.id
        assert exc.value.duplicate_window == "1h_similar"


def test_seven_day_semantic_duplicate_blocks_later_rephrased_message():
    now = _now()
    with _session() as session:
        first = reserve_group_ai_message(
            session,
            tenant_id=1,
            group_id=22,
            task_id="task-1",
            account_id=101,
            raw_text="主任这个可以先问价格",
            now=now,
        )
        mark_group_ai_message_result(session, first.id, status="unknown_after_send", action_id="action-1")
        session.commit()

        with pytest.raises(DuplicateMessageReservation) as exc:
            reserve_group_ai_message(
                session,
                tenant_id=1,
                group_id=22,
                task_id="task-1",
                account_id=102,
                raw_text="主任价格这个先问一下可以",
                now=now + timedelta(days=2),
            )

        assert exc.value.reference_id == first.id
        assert exc.value.duplicate_window == "7d_semantic"


def test_thirty_day_template_shell_limits_vague_summary_phrase():
    now = _now()
    with _session() as session:
        first = reserve_group_ai_message(
            session,
            tenant_id=1,
            group_id=22,
            task_id="task-1",
            account_id=101,
            raw_text="这个确实不错，感觉挺靠谱",
            now=now,
        )
        mark_group_ai_message_result(session, first.id, status="success", action_id="action-1")
        session.commit()

        with pytest.raises(DuplicateMessageReservation) as exc:
            reserve_group_ai_message(
                session,
                tenant_id=1,
                group_id=22,
                task_id="task-1",
                account_id=102,
                raw_text="这个确实可以，感觉挺靠谱",
                now=now + timedelta(days=20),
            )

        assert exc.value.reference_id == first.id
        assert exc.value.duplicate_window == "30d_template_shell"


def test_final_sendable_check_excludes_self_but_blocks_other_duplicate_memory():
    now = _now()
    with _session() as session:
        current = reserve_group_ai_message(
            session,
            tenant_id=1,
            group_id=22,
            task_id="task-1",
            account_id=101,
            raw_text="花花老师身材服务真好",
            now=now,
        )
        ensure_group_ai_message_sendable(session, current.id, now=now + timedelta(minutes=1))
        conflict = AiGroupMessageMemory(
            tenant_id=1,
            group_id=22,
            task_id="other-task",
            account_id=102,
            raw_text="花花老师服务身材真好",
            normalized_text=normalize_group_ai_text("花花老师服务身材真好"),
            text_fingerprint="manual-conflict",
            semantic_cluster="manual-conflict",
            reservation_key="manual-conflict",
            status="success",
            planned_at=now + timedelta(seconds=30),
        )
        session.add(conflict)
        session.commit()

        with pytest.raises(DuplicateMessageReservation) as exc:
            ensure_group_ai_message_sendable(session, current.id, now=now + timedelta(minutes=1))

        assert exc.value.reference_id == conflict.id
        assert exc.value.duplicate_window == "1h_similar"


def test_backfill_group_ai_message_memory_from_success_and_unknown_actions():
    now = _now()
    with _session() as session:
        session.add_all(
            [
                Action(
                    id="action-success",
                    tenant_id=1,
                    task_id="task-1",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=101,
                    status="success",
                    scheduled_at=now - timedelta(days=2),
                    executed_at=now - timedelta(days=2),
                    payload={
                        "group_id": 22,
                        "message_text": "花花老师身材服务真好",
                        "topic_direction": {"title": "郑州楼凤妹子怎么样"},
                        "teacher_target": {"name": "花花老师"},
                        "profile_version": 3,
                    },
                    result={"remote_message_id": "tg-1"},
                ),
                Action(
                    id="action-unknown",
                    tenant_id=1,
                    task_id="task-1",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=102,
                    status="unknown_after_send",
                    scheduled_at=now - timedelta(days=1),
                    payload={"group_id": 22, "message_text": "主任这个可以先问价格"},
                ),
                Action(
                    id="action-invalid",
                    tenant_id=1,
                    task_id="task-1",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=103,
                    status="success",
                    scheduled_at=now - timedelta(days=1),
                    payload={"message_text": "缺少群 id"},
                ),
            ]
        )
        session.commit()

        result = backfill_group_ai_message_memory_from_actions(session, tenant_id=1, now=now)
        session.commit()

        rows = list(session.scalars(select(AiGroupMessageMemory).order_by(AiGroupMessageMemory.action_id)))
        assert result == {"created": 2, "skipped_existing": 0, "skipped_invalid": 1}
        assert [row.action_id for row in rows] == ["action-success", "action-unknown"]
        assert rows[0].status == "success"
        assert rows[0].sent_at == now - timedelta(days=2)
        assert rows[0].topic_direction == "郑州楼凤妹子怎么样"
        assert rows[0].teacher_target == "花花老师"
        assert rows[0].profile_version == 3
        assert rows[0].reservation_key == ""
        assert rows[1].status == "unknown_after_send"

        with pytest.raises(DuplicateMessageReservation) as exc:
            reserve_group_ai_message(
                session,
                tenant_id=1,
                group_id=22,
                task_id="task-new",
                account_id=104,
                raw_text="花花老师服务身材挺好",
                now=now,
            )

        assert exc.value.reference_id == rows[0].id
        assert exc.value.duplicate_window == "7d_semantic"


def test_backfill_group_ai_message_memory_is_idempotent_by_action_id():
    now = _now()
    with _session() as session:
        session.add(
            Action(
                id="action-success",
                tenant_id=1,
                task_id="task-1",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=101,
                status="success",
                scheduled_at=now - timedelta(days=1),
                executed_at=now - timedelta(days=1),
                payload={"group_id": 22, "message_text": "精品榜的妹子真好"},
            )
        )
        session.commit()

        assert backfill_group_ai_message_memory_from_actions(session, tenant_id=1, now=now)["created"] == 1
        session.commit()
        second = backfill_group_ai_message_memory_from_actions(session, tenant_id=1, now=now)
        session.commit()

        assert second == {"created": 0, "skipped_existing": 1, "skipped_invalid": 0}
        assert session.query(AiGroupMessageMemory).count() == 1


def test_backfill_group_ai_message_memory_uses_sent_time_window():
    now = _now()
    with _session() as session:
        session.add(
            Action(
                id="action-recent-send-old-create",
                tenant_id=1,
                task_id="task-ai",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=101,
                status="success",
                created_at=now - timedelta(days=60),
                scheduled_at=now - timedelta(days=2),
                executed_at=now - timedelta(days=2),
                payload={"group_id": 22, "message_text": "这个妹子反馈挺稳"},
            )
        )
        session.commit()

        result = backfill_group_ai_message_memory_from_actions(session, tenant_id=1, now=now)
        session.commit()

        assert result["created"] == 1
        memory = session.query(AiGroupMessageMemory).one()
        assert memory.action_id == "action-recent-send-old-create"
        assert memory.planned_at == now - timedelta(days=2)


def test_ai_message_memory_maintenance_expires_and_backfills_all_tenants():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, future=True)
    now = _now()

    with session_factory() as session:
        session.add(
            AiGroupMessageMemory(
                tenant_id=1,
                group_id=22,
                task_id="task-ai",
                account_id=101,
                raw_text="旧预占",
                normalized_text=normalize_group_ai_text("旧预占"),
                text_fingerprint="old-reservation",
                semantic_cluster="old-reservation",
                reservation_key="old-reservation",
                status="reserved",
                planned_at=now - timedelta(hours=1),
                expires_at=now - timedelta(minutes=10),
            )
        )
        session.add_all(
            [
                Action(
                    id="action-tenant-1",
                    tenant_id=1,
                    task_id="task-ai",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=101,
                    status="success",
                    scheduled_at=now - timedelta(days=1),
                    payload={"group_id": 22, "message_text": "花花老师身材服务真好"},
                ),
                Action(
                    id="action-tenant-2",
                    tenant_id=2,
                    task_id="task-ai",
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=201,
                    status="unknown_after_send",
                    scheduled_at=now - timedelta(days=1),
                    payload={"group_id": 33, "message_text": "这个可以先问价格"},
                ),
            ]
        )
        session.commit()

    processed = drain_ai_message_memory_maintenance(session_factory, limit=10, now=now)

    with session_factory() as session:
        rows = session.query(AiGroupMessageMemory).order_by(AiGroupMessageMemory.tenant_id, AiGroupMessageMemory.action_id).all()
        assert processed == 3
        assert [row.status for row in rows] == ["expired_before_send", "success", "unknown_after_send"]
        assert [row.tenant_id for row in rows] == [1, 1, 2]
