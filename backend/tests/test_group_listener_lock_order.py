from types import SimpleNamespace

import pytest

from app.services import group_listener_context_writer as writer


pytestmark = pytest.mark.no_postgres


def test_listener_locks_speaker_before_processing_admission_events(monkeypatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(
        writer,
        "_lock_group_speaker_state",
        lambda *_args, **_kwargs: order.append("speaker"),
    )
    monkeypatch.setattr(
        writer,
        "_process_group_bot_control_event",
        lambda *_args, **_kwargs: order.append("admission"),
    )
    monkeypatch.setattr(writer, "_refresh_existing_control_buttons", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(writer, "_record_speaker_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(writer, "_context_message", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(writer, "_maybe_apply_legacy_required_channel_prompt", lambda *_args, **_kwargs: None)

    writer.insert_context_snapshots(
        SimpleNamespace(scalar=lambda _statement: None),
        SimpleNamespace(id=7, tenant_id=1),
        SimpleNamespace(),
        [SimpleNamespace()],
        ignored_sender=lambda _snapshot: False,
        create_source_media=False,
        learning_scene=None,
    )

    assert order == ["speaker", "admission"]
