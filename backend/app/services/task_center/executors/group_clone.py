from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.group_clone import (
    CloneAlbumItem,
    CloneAlbumManifest,
    CloneDeliveryObligation,
    CloneSourceEvent,
)
from app.models.task_center import Task
from app.services.task_center.group_clone_binding import CloneSenderBindingManager
from app.services.task_center.group_clone_identity import derive_deterministic_random_id
from app.services.task_center.group_clone_materializer import materialize_ready_clone_events
from app.services.task_center.group_clone_source_stream import consume_clone_deliveries


# ---------------------------------------------------------------------------
# 3. 相册聚合器 (§8.2)
# ---------------------------------------------------------------------------
class CloneAlbumAggregator:
    @staticmethod
    def process_album_item(
        session: Session,
        task: Task,
        source_event: CloneSourceEvent,
        *,
        quiet_seconds: float = 1.5,
        max_seconds: float = 10.0,
    ) -> Tuple[CloneAlbumManifest, bool]:
        """
        聚合相册分片，判断是否静默窗口就绪。
        """
        grouped_id = source_event.grouped_id
        if not grouped_id:
            raise ValueError("source_event does not have grouped_id")

        current_time = datetime.now(timezone.utc)
        manifest = _locked_album_manifest(session, task, grouped_id)
        if manifest is None:
            manifest = _new_album_manifest(
                session, task, grouped_id=grouped_id, current_time=current_time,
                quiet_seconds=quiet_seconds, max_seconds=max_seconds,
            )
        item = session.scalar(select(CloneAlbumItem).where(
            CloneAlbumItem.manifest_id == manifest.id,
            CloneAlbumItem.source_message_id == source_event.source_message_id,
        ))
        if item is None:
            _append_album_item(
                session, manifest, source_event=source_event,
                current_time=current_time, quiet_seconds=quiet_seconds,
            )
        return manifest, (manifest.state == "ready")


# ---------------------------------------------------------------------------
# 4. Sequencer 调度器与时序保序 (§9.1, §9.2)
# ---------------------------------------------------------------------------
class CloneSequencer:
    @staticmethod
    def calculate_human_planned_at(
        session: Session,
        task: Task,
        *,
        stream_order_no: int,
        delay_min_seconds: float = 3.0,
        delay_max_seconds: float = 8.0,
    ) -> datetime:
        """
        基于前序最大 planned_at 单调递增拟人随机延迟。
        """
        current_time = datetime.now(timezone.utc)
        stmt = (
            select(func.max(CloneDeliveryObligation.planned_at))
            .where(
                CloneDeliveryObligation.task_id == task.id,
                CloneDeliveryObligation.epoch == task.task_lifecycle_epoch,
                CloneDeliveryObligation.stream_order_no < stream_order_no,
            )
        )
        prev_max = session.execute(stmt).scalar_one_or_none()

        if prev_max:
            if prev_max.tzinfo is None:
                prev_max = prev_max.replace(tzinfo=timezone.utc)
            base_time = max(current_time, prev_max)
        else:
            base_time = current_time

        jitter = random.uniform(delay_min_seconds, delay_max_seconds)
        return base_time + timedelta(seconds=jitter)


def _locked_album_manifest(session, task, grouped_id):
    return session.scalar(select(CloneAlbumManifest).where(
        CloneAlbumManifest.task_id == task.id,
        CloneAlbumManifest.epoch == task.task_lifecycle_epoch,
        CloneAlbumManifest.grouped_id == grouped_id,
    ).with_for_update())


def _new_album_manifest(session, task, *, grouped_id, current_time, quiet_seconds, max_seconds):
    manifest = CloneAlbumManifest(
        task_id=task.id, epoch=task.task_lifecycle_epoch, grouped_id=grouped_id,
        first_observed_at=current_time, last_observed_at=current_time,
        quiet_deadline_at=current_time + timedelta(seconds=quiet_seconds),
        max_deadline_at=current_time + timedelta(seconds=max_seconds),
        items_total=0, state="collecting",
    )
    session.add(manifest)
    session.flush()
    return manifest


def _append_album_item(session, manifest, *, source_event, current_time, quiet_seconds):
    session.add(CloneAlbumItem(
        manifest_id=manifest.id, source_event_id=source_event.id,
        part_index=manifest.items_total, source_message_id=source_event.source_message_id,
        media_type=source_event.media_type or "photo", media_snapshot={},
        item_fingerprint=source_event.content_fingerprint, acquisition_state="acquired",
    ))
    manifest.last_observed_at = current_time
    manifest.quiet_deadline_at = current_time + timedelta(seconds=quiet_seconds)
    manifest.items_total += 1
    manifest.version += 1
    session.flush()


# ---------------------------------------------------------------------------
# 5. Task Center Executor 主入口 (§18.1)
# ---------------------------------------------------------------------------
class GroupCloneExecutor:
    def build_plan(self, session: Session, task: Task) -> int:
        consume_clone_deliveries(session, task)
        return materialize_ready_clone_events(session, task)


group_clone = GroupCloneExecutor()


def build_plan(session: Session, task: Task) -> int:
    return group_clone.build_plan(session, task)

__all__ = [
    "CloneAlbumAggregator",
    "CloneSenderBindingManager",
    "CloneSequencer",
    "GroupCloneExecutor",
    "derive_deterministic_random_id",
    "build_plan",
    "group_clone",
]
