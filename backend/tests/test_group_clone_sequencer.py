from __future__ import annotations

from datetime import datetime, timezone
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.no_postgres

from app.database import Base
from app.models import Tenant, Task
from app.models.group_clone import CloneAlbumManifest, CloneDeliveryObligation, CloneSourceEvent
from app.services.task_center.executors.group_clone import (
    CloneAlbumAggregator,
    CloneSequencer,
    derive_deterministic_random_id,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()

    tenant = Tenant(id=1, name="Tenant 1")
    session.add(tenant)
    session.flush()

    task = Task(
        id="task-seq-1",
        tenant_id=1,
        name="Sequencer Test Task",
        type="group_clone",
        status="running",
        task_lifecycle_epoch=1,
    )
    session.add(task)
    session.commit()

    yield session
    session.close()


def test_deterministic_random_id():
    kwargs = {"task_id": "task-1", "epoch": 1, "obligation_id": "obl-100", "mutation_kind": "sendMessage"}
    id1 = derive_deterministic_random_id("v2_group_clone", 1, part_index=0, **kwargs)
    id2 = derive_deterministic_random_id("v2_group_clone", 1, part_index=0, **kwargs)
    id3 = derive_deterministic_random_id("v2_group_clone", 1, part_index=1, **kwargs)

    assert id1 == id2
    assert id1 != id3
    assert isinstance(id1, int)
    assert id1 != 0
    # 必须在 signed 64-bit 范围内
    assert -9223372036854775808 <= id1 <= 9223372036854775807


def test_album_aggregator_quiet_window(db_session: Session):
    task = db_session.get(Task, "task-seq-1")
    ev1 = CloneSourceEvent(
        tenant_id=1,
        task_id=task.id,
        task_lifecycle_epoch=1,
        source_peer_type="channel",
        source_peer_id="-100111",
        source_message_id=201,
        event_type="message_new",
        event_identity_hash="ev201",
        apply_order_key="201",
        stream_order_no=1,
        grouped_id="album_grp_999",
        media_type="photo",
        content_fingerprint="fp201",
    )
    db_session.add(ev1)
    db_session.flush()

    manifest, ready = CloneAlbumAggregator.process_album_item(db_session, task, ev1, quiet_seconds=1.5)
    assert manifest.items_total == 1
    assert manifest.state == "collecting"
    assert not ready
    duplicate, _ = CloneAlbumAggregator.process_album_item(db_session, task, ev1, quiet_seconds=1.5)
    assert duplicate.items_total == 1

    # 收到同一相册第 2 张照片
    ev2 = CloneSourceEvent(
        tenant_id=1,
        task_id=task.id,
        task_lifecycle_epoch=1,
        source_peer_type="channel",
        source_peer_id="-100111",
        source_message_id=202,
        event_type="message_new",
        event_identity_hash="ev202",
        apply_order_key="202",
        stream_order_no=2,
        grouped_id="album_grp_999",
        media_type="photo",
        content_fingerprint="fp202",
    )
    db_session.add(ev2)
    db_session.flush()

    manifest2, _ = CloneAlbumAggregator.process_album_item(db_session, task, ev2, quiet_seconds=1.5)
    assert manifest2.id == manifest.id
    assert manifest2.items_total == 2


def test_sequencer_monotonic_planned_at(db_session: Session):
    task = db_session.get(Task, "task-seq-1")
    t1 = CloneSequencer.calculate_human_planned_at(db_session, task, stream_order_no=1, delay_min_seconds=3.0, delay_max_seconds=5.0)

    # 插入第 1 条 obligation
    obl1 = CloneDeliveryObligation(
        tenant_id=1,
        task_id=task.id,
        epoch=1,
        source_event_id="dummy_ev_1",
        obligation_kind="send",
        stream_order_no=1,
        sequencer_id=1,
        planned_at=t1,
        state="ready",
    )
    db_session.add(obl1)
    db_session.commit()

    # 计算第 2 条消息的 planned_at，必须严格晚于 t1
    t2 = CloneSequencer.calculate_human_planned_at(db_session, task, stream_order_no=2, delay_min_seconds=3.0, delay_max_seconds=5.0)
    assert t2 >= t1
    assert (t2 - t1).total_seconds() >= 3.0
