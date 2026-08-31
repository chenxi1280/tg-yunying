from __future__ import annotations

from typing import Any

from .contracts import SendResult
from .telethon_utils import resolve_telethon_target


async def send_clone_media(
    source_client: Any,
    target_client: Any,
    *,
    source_peer_id: str,
    target_peer_id: str,
    items: list[dict],
    reply_to_message_id: int | None,
    target_top_message_id: int | None,
) -> SendResult:
    from telethon import functions, types

    try:
        source = await resolve_telethon_target(source_client, source_peer_id)
        target = await resolve_telethon_target(target_client, target_peer_id)
        source_messages = await _source_messages(source_client, source, items)
        media = await _prepare_media(
            source_client, target_client, types, items, source_messages,
        )
        reply = _reply_header(types, reply_to_message_id, target_top_message_id)
    except Exception as exc:
        return SendResult(
            False, failure_type=type(exc).__name__, detail=str(exc),
            remote_mutation_started=False,
        )
    try:
        result = await _send_media_request(
            target_client, functions, target, media, reply,
        )
    except Exception as exc:
        return SendResult(
            False, failure_type=type(exc).__name__, detail=str(exc),
            remote_mutation_started=True,
        )
    remote_ids = _remote_ids(result, [int(item["random_id"]) for item in items])
    if len(remote_ids) != len(items):
        return SendResult(
            False,
            failure_type="group_clone_media_result_unknown",
            detail="媒体 RPC 已返回但缺少完整 UpdateMessageID 映射",
            remote_mutation_started=True,
        )
    return SendResult(
        True,
        remote_message_id=remote_ids[0],
        remote_message_ids=tuple(remote_ids),
        remote_mutation_started=True,
    )


async def _send_media_request(client, functions, target, media, reply):
    if len(media) == 1:
        item = media[0]
        return await client(functions.messages.SendMediaRequest(
            peer=target, media=item.media, message=item.message,
            random_id=item.random_id, entities=item.entities, reply_to=reply,
        ))
    return await client(functions.messages.SendMultiMediaRequest(
        peer=target, multi_media=media, reply_to=reply,
    ))


async def _prepare_media(source_client, target_client, types, items, source_messages):
    result = []
    for item in items:
        input_media = await _input_media(
            source_client, target_client, types, item=item,
            source_message=source_messages.get(int(item["source_message_id"])),
        )
        result.append(types.InputSingleMedia(
            media=input_media,
            message=str(item.get("content") or ""),
            random_id=int(item["random_id"]),
            entities=_message_entities(types, item),
        ))
    return result


async def _source_messages(client, source, items) -> dict[int, Any]:
    ids = [
        int(item["source_message_id"])
        for item in items
        if str(item.get("media_type") or "") != "poll"
    ]
    if not ids:
        return {}
    rows = await client.get_messages(source, ids=ids)
    result = {int(row.id): row for row in rows if row is not None}
    if set(ids) != set(result):
        raise RuntimeError("group_clone_media_source_message_missing")
    return result


async def _input_media(source_client, target_client, types, *, item, source_message):
    media_type = str(item.get("media_type") or "")
    if media_type == "poll":
        return _poll_media(types, dict(item.get("poll_snapshot") or {}))
    if source_message is None or getattr(source_message, "media", None) is None:
        raise RuntimeError("group_clone_media_source_changed")
    data = await source_client.download_media(source_message, bytes)
    if not data:
        raise RuntimeError("group_clone_media_download_empty")
    filename = _filename(source_message, media_type)
    uploaded = await target_client.upload_file(data, file_name=filename)
    if media_type == "photo":
        return types.InputMediaUploadedPhoto(file=uploaded)
    document = getattr(source_message.media, "document", None)
    attributes = list(getattr(document, "attributes", None) or ())
    mime_type = str(getattr(document, "mime_type", "") or "application/octet-stream")
    return types.InputMediaUploadedDocument(
        file=uploaded,
        mime_type=mime_type,
        attributes=attributes,
        force_file=media_type == "document",
    )


def _poll_media(types, snapshot: dict):
    question = str(snapshot.get("question") or "").strip()
    answers = list(snapshot.get("answers") or [])
    if not question or len(answers) < 2:
        raise RuntimeError("group_clone_poll_snapshot_invalid")
    text = lambda value: types.TextWithEntities(text=str(value), entities=[])
    poll_answers = [
        types.PollAnswer(text=text(item.get("text") or ""), option=bytes.fromhex(str(item.get("option") or "")))
        for item in answers
    ]
    poll = types.Poll(
        id=0, question=text(question), answers=poll_answers, hash=0,
        closed=bool(snapshot.get("closed", False)),
        public_voters=bool(snapshot.get("public_voters", False)),
        multiple_choice=bool(snapshot.get("multiple_choice", False)),
        quiz=bool(snapshot.get("quiz", False)),
    )
    correct_answers = [int(value) for value in (snapshot.get("correct_answer_indices") or ())]
    if any(value < 0 or value >= len(answers) for value in correct_answers):
        raise RuntimeError("group_clone_poll_correct_answer_invalid")
    if bool(snapshot.get("quiz", False)) and not correct_answers:
        raise RuntimeError("group_clone_poll_quiz_answer_unproven")
    return types.InputMediaPoll(
        poll=poll,
        correct_answers=correct_answers or None,
    )


def _message_entities(types, item):
    from .gateway import _telethon_message_entities

    return _telethon_message_entities(
        types, str(item.get("content") or ""), list(item.get("entities") or ()),
    )


def _reply_header(types, reply_to_message_id, target_top_message_id):
    if reply_to_message_id is None and target_top_message_id is None:
        return None
    return types.InputReplyToMessage(
        reply_to_msg_id=reply_to_message_id or 0,
        top_msg_id=target_top_message_id,
    )


def _remote_ids(result, random_ids: list[int]) -> list[str]:
    mappings = {
        int(update.random_id): str(update.id)
        for update in (getattr(result, "updates", None) or ())
        if type(update).__name__ == "UpdateMessageID"
    }
    return [mappings[value] for value in random_ids if value in mappings]


def _filename(message, media_type: str) -> str:
    document = getattr(getattr(message, "media", None), "document", None)
    for attribute in getattr(document, "attributes", None) or ():
        name = str(getattr(attribute, "file_name", "") or "")
        if name:
            return name
    suffix = "jpg" if media_type == "photo" else "bin"
    return f"clone-{int(message.id)}.{suffix}"


__all__ = ["send_clone_media"]
