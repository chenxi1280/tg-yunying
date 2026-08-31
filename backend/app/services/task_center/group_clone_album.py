from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.group_clone import CloneAlbumItem, CloneAlbumManifest, CloneSourceEvent

ALBUM_QUIET_SECONDS = 2
ALBUM_MAX_WAIT_SECONDS = 10


def prepare_album_events(
    session: Session,
    task,
    *,
    event,
    obligation,
    incomplete_policy: str,
) -> list[CloneSourceEvent] | None:
    manifest = _manifest(session, task, event.grouped_id)
    if manifest is None:
        manifest = _new_manifest(session, task, event.grouped_id, incomplete_policy)
    obligation.album_manifest_id = manifest.id
    if _is_late_frozen_part(session, manifest, event):
        obligation.state = "waiting_manual_review"
        obligation.error_code = "album_late_part_after_freeze"
        return None
    _collect_known_items(session, task, manifest)
    events = _album_events(session, task, manifest)
    if events and event.id != events[0].id:
        obligation.state = "superseded"
        obligation.resolved_at = _now()
        return []
    current = _now()
    if manifest.state == "incomplete_timeout":
        return _settle_incomplete(obligation, manifest, events)
    if current < _aware(manifest.quiet_deadline_at):
        _wait(obligation, "album_quiet_window_open")
        return None
    if len(events) >= 2:
        _freeze_manifest(manifest, events)
        return events
    if current < _aware(manifest.max_deadline_at):
        _wait(obligation, "album_collecting_single_item")
        return None
    manifest.state = "incomplete_timeout"
    manifest.version += 1
    return _settle_incomplete(obligation, manifest, events)


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


def _is_late_frozen_part(session, manifest, event) -> bool:
    if manifest.state in {"collecting"}:
        return False
    existing = session.scalar(select(CloneAlbumItem.id).where(
        CloneAlbumItem.manifest_id == manifest.id,
        CloneAlbumItem.source_event_id == event.id,
    ))
    return existing is None


def _collect_known_items(session, task, manifest) -> None:
    events = session.scalars(select(CloneSourceEvent).where(
        CloneSourceEvent.task_id == task.id,
        CloneSourceEvent.task_lifecycle_epoch == task.task_lifecycle_epoch,
        CloneSourceEvent.grouped_id == manifest.grouped_id,
    ).order_by(CloneSourceEvent.stream_order_no)).all()
    existing = set(session.scalars(select(CloneAlbumItem.source_event_id).where(
        CloneAlbumItem.manifest_id == manifest.id,
    )))
    previous_last_observed = _aware(manifest.last_observed_at)
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
    if _aware(manifest.last_observed_at) > previous_last_observed:
        manifest.quiet_deadline_at = min(
            _aware(manifest.last_observed_at) + timedelta(seconds=ALBUM_QUIET_SECONDS),
            _aware(manifest.max_deadline_at),
        )
    session.flush()


def _settle_incomplete(obligation, manifest, events) -> list[CloneSourceEvent]:
    if manifest.frozen_policy == "drop_incomplete":
        obligation.state = "filtered"
        obligation.error_code = "album_incomplete"
        obligation.resolved_at = _now()
        return []
    manifest.state = "ready_partial_degraded"
    obligation.degradation_reason = "album_incomplete"
    _freeze_manifest(manifest, events, state="ready_partial_degraded")
    return events


def _wait(obligation, code: str) -> None:
    obligation.state = "waiting_album"
    obligation.error_code = code


def _album_events(session, task, manifest):
    return list(session.scalars(select(CloneSourceEvent).where(
        CloneSourceEvent.task_id == task.id,
        CloneSourceEvent.task_lifecycle_epoch == task.task_lifecycle_epoch,
        CloneSourceEvent.grouped_id == manifest.grouped_id,
    ).order_by(CloneSourceEvent.stream_order_no)))


def _freeze_manifest(manifest, events, *, state="ready") -> None:
    identity = [
        (item.source_message_id, item.media_type, item.content_fingerprint)
        for item in events
    ]
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    manifest.collection_fingerprint = hashlib.sha256(raw.encode()).hexdigest()
    manifest.state = state
    manifest.version += 1


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = ["prepare_album_events"]
