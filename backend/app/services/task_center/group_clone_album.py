from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.group_clone import CloneAlbumItem, CloneAlbumManifest, CloneSourceEvent

ALBUM_QUIET_SECONDS = 2
ALBUM_MAX_WAIT_SECONDS = 10


def prepare_album_obligation(
    session: Session,
    task,
    *,
    event,
    obligation,
    incomplete_policy: str,
) -> bool:
    manifest = _manifest(session, task, event.grouped_id)
    if manifest is None:
        manifest = _new_manifest(session, task, event.grouped_id, incomplete_policy)
    obligation.album_manifest_id = manifest.id
    _collect_known_items(session, task, manifest)
    current = _now()
    if manifest.state == "incomplete_timeout":
        return _settle_incomplete(obligation, manifest)
    if current < _aware(manifest.quiet_deadline_at):
        return _wait(obligation, "album_quiet_window_open")
    manifest.state = "verifying_source"
    if current < _aware(manifest.max_deadline_at):
        return _wait(obligation, "album_fresh_refetch_pending")
    manifest.state = "incomplete_timeout"
    manifest.version += 1
    return _settle_incomplete(obligation, manifest)


def _manifest(session, task, grouped_id):
    return session.scalar(select(CloneAlbumManifest).where(
        CloneAlbumManifest.task_id == task.id,
        CloneAlbumManifest.epoch == task.task_lifecycle_epoch,
        CloneAlbumManifest.grouped_id == grouped_id,
    ).with_for_update())


def _new_manifest(session, task, grouped_id, policy):
    current = _now()
    manifest = CloneAlbumManifest(
        task_id=task.id,
        epoch=task.task_lifecycle_epoch,
        grouped_id=grouped_id,
        first_observed_at=current,
        last_observed_at=current,
        quiet_deadline_at=current + timedelta(seconds=ALBUM_QUIET_SECONDS),
        max_deadline_at=current + timedelta(seconds=ALBUM_MAX_WAIT_SECONDS),
        frozen_policy=policy,
    )
    session.add(manifest)
    session.flush()
    return manifest


def _collect_known_items(session, task, manifest) -> None:
    events = session.scalars(select(CloneSourceEvent).where(
        CloneSourceEvent.task_id == task.id,
        CloneSourceEvent.task_lifecycle_epoch == task.task_lifecycle_epoch,
        CloneSourceEvent.grouped_id == manifest.grouped_id,
    ).order_by(CloneSourceEvent.stream_order_no)).all()
    existing = set(session.scalars(select(CloneAlbumItem.source_event_id).where(
        CloneAlbumItem.manifest_id == manifest.id,
    )))
    for event in events:
        if event.id in existing:
            continue
        session.add(CloneAlbumItem(
            manifest_id=manifest.id,
            source_event_id=event.id,
            part_index=manifest.items_total,
            source_message_id=event.source_message_id,
            media_type=event.media_type or "unknown",
            media_snapshot={},
            item_fingerprint=event.content_fingerprint,
            acquisition_state="metadata_only",
        ))
        manifest.items_total += 1
        manifest.last_observed_at = max(
            _aware(manifest.last_observed_at), _aware(event.observed_at),
        )
        manifest.version += 1
    session.flush()


def _settle_incomplete(obligation, manifest) -> bool:
    if manifest.frozen_policy == "drop_incomplete":
        obligation.state = "filtered"
        obligation.error_code = "album_incomplete"
        obligation.resolved_at = _now()
        return False
    obligation.state = "waiting_manual_review"
    obligation.error_code = "album_partial_send_adapter_required"
    return False


def _wait(obligation, code: str) -> bool:
    obligation.state = "waiting_album"
    obligation.error_code = code
    return False


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = ["prepare_album_obligation"]
