from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from .update_contracts import (
    TelegramDifferenceBatch,
    TelegramNormalizedUpdate,
    TelegramOutboundMessageMapping,
)
from .telethon_utils import resolve_telethon_target

DIFFERENCE_LIMIT = 100


async def fetch_update_state(client: Any) -> TelegramDifferenceBatch:
    from telethon import functions

    state = await client(functions.updates.GetStateRequest())
    return TelegramDifferenceBatch(
        scope="common",
        status="empty",
        cursor=_state_cursor(state),
    )


async def fetch_update_difference(
    client: Any,
    *,
    pts: int,
    qts: int,
    date: int,
) -> TelegramDifferenceBatch:
    from telethon import functions

    result = await client(functions.updates.GetDifferenceRequest(
        pts=pts,
        qts=qts,
        date=_cursor_date(date),
        pts_limit=DIFFERENCE_LIMIT,
        qts_limit=DIFFERENCE_LIMIT,
    ))
    return _common_difference(result, requested_pts=pts)


async def fetch_channel_difference(
    client: Any,
    *,
    peer_id: str,
    pts: int,
) -> TelegramDifferenceBatch:
    from telethon import functions, types

    target = await resolve_telethon_target(client, peer_id)
    result = await client(functions.updates.GetChannelDifferenceRequest(
        channel=target,
        filter=types.ChannelMessagesFilterEmpty(),
        pts=pts,
        limit=DIFFERENCE_LIMIT,
        force=True,
    ))
    return _channel_difference(result, peer_id=peer_id, requested_pts=pts)


def _common_difference(result: Any, *, requested_pts: int) -> TelegramDifferenceBatch:
    name = type(result).__name__
    if name == "DifferenceTooLong":
        return TelegramDifferenceBatch(
            scope="common",
            status="too_long",
            cursor={"pts": int(result.pts)},
            final=False,
        )
    if name == "DifferenceEmpty":
        return TelegramDifferenceBatch(
            scope="common",
            status="empty",
            cursor={"date": _unix(result.date), "seq": int(result.seq or 0)},
        )
    state = getattr(result, "state", None) or getattr(result, "intermediate_state", None)
    status = "slice" if name == "DifferenceSlice" else "live"
    updates, mappings = _normalize_container(
        getattr(result, "new_messages", ()) or (),
        getattr(result, "other_updates", ()) or (),
        fallback_peer_id=None,
        requested_pts=requested_pts,
        result_pts=int(getattr(state, "pts", 0) or 0),
        entities=[*(getattr(result, "users", ()) or ()), *(getattr(result, "chats", ()) or ())],
    )
    return TelegramDifferenceBatch(
        scope="common",
        status=status,
        cursor=_state_cursor(state),
        updates=updates,
        outbound_mappings=mappings,
        final=name != "DifferenceSlice",
    )


def _channel_difference(result: Any, *, peer_id: str, requested_pts: int) -> TelegramDifferenceBatch:
    name = type(result).__name__
    result_pts = _channel_result_pts(result)
    status = _channel_status(name, result)
    messages = getattr(result, "new_messages", None)
    if messages is None:
        messages = getattr(result, "messages", ()) or ()
    updates, mappings = _normalize_container(
        messages,
        getattr(result, "other_updates", ()) or (),
        fallback_peer_id=peer_id,
        requested_pts=requested_pts,
        result_pts=result_pts,
        entities=[*(getattr(result, "users", ()) or ()), *(getattr(result, "chats", ()) or ())],
    )
    return TelegramDifferenceBatch(
        scope="channel",
        status=status,
        cursor={"pts": result_pts},
        updates=updates,
        outbound_mappings=mappings,
        final=bool(getattr(result, "final", True)) and status != "too_long",
    )


def _normalize_container(
    messages: Any,
    updates: Any,
    *,
    fallback_peer_id: str | None,
    requested_pts: int,
    result_pts: int,
    entities: Any = (),
) -> tuple[tuple[TelegramNormalizedUpdate, ...], tuple[TelegramOutboundMessageMapping, ...]]:
    envelopes: list[TelegramNormalizedUpdate] = []
    mappings: list[TelegramOutboundMessageMapping] = []
    sender_names = _sender_names(entities)
    sender_bots = _sender_bot_flags(entities)
    message_items = [
        _normalized_message(
            item,
            event_type="message_new",
            sender_names=sender_names,
            sender_bots=sender_bots,
        )
        for item in messages
    ]
    message_items = [item for item in message_items if item is not None]
    for peer_id, peer_items in _message_groups(message_items, fallback_peer_id=fallback_peer_id):
        envelopes.append(_difference_messages_envelope(
            peer_items,
            peer_id=peer_id,
            requested_pts=requested_pts,
            result_pts=result_pts,
        ))
    for update in updates:
        envelope, mapping = _normalize_update(
            update, sender_names=sender_names, sender_bots=sender_bots,
        )
        if envelope is not None:
            envelopes.append(envelope)
        if mapping is not None:
            mappings.append(mapping)
    return tuple(envelopes), tuple(mappings)


def _message_groups(
    items: list[dict[str, Any]],
    *,
    fallback_peer_id: str | None,
) -> list[tuple[str, list[dict[str, Any]]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        peer_id = fallback_peer_id or str(item["routing_peer_id"])
        groups.setdefault(peer_id, []).append(item)
    return list(groups.items())


def _normalize_update(
    update: Any,
    *,
    sender_names: dict[tuple[str, str], str],
    sender_bots: dict[tuple[str, str], bool],
) -> tuple[TelegramNormalizedUpdate | None, TelegramOutboundMessageMapping | None]:
    name = type(update).__name__
    if name == "UpdateMessageID":
        identity = f"outbound:{int(update.random_id)}:{int(update.id)}"
        return (
            TelegramNormalizedUpdate(identity, name),
            TelegramOutboundMessageMapping(int(update.random_id), int(update.id), identity),
        )
    if name in {"UpdateNewChannelMessage", "UpdateNewMessage", "UpdateEditChannelMessage", "UpdateEditMessage"}:
        event_type = "message_edit" if "Edit" in name else "message_new"
        item = _normalized_message(
            update.message,
            event_type=event_type,
            sender_names=sender_names,
            sender_bots=sender_bots,
        )
        return (_message_update_envelope(update, item) if item else None, None)
    if name == "UpdateDeleteChannelMessages":
        return (_ids_update_envelope(update, event_type="message_delete", pinned=None), None)
    if name == "UpdatePinnedChannelMessages":
        return (_ids_update_envelope(update, event_type="message_pin", pinned=bool(update.pinned)), None)
    return (_minimal_update_envelope(update), None)


def _message_update_envelope(update: Any, item: dict[str, Any]) -> TelegramNormalizedUpdate:
    pts = int(getattr(update, "pts", 0) or 0)
    identity = _message_identity(item, pts=pts)
    peer_type = str(item.pop("routing_peer_type"))
    peer_id = str(item.pop("routing_peer_id"))
    return TelegramNormalizedUpdate(
        identity_key=identity,
        constructor_name=type(update).__name__,
        pts=pts or None,
        pts_count=int(getattr(update, "pts_count", 0) or 0) or None,
        routing_peer_type=peer_type,
        routing_peer_id=peer_id,
        normalized_items=(item,),
    )


def _ids_update_envelope(update: Any, *, event_type: str, pinned: bool | None) -> TelegramNormalizedUpdate:
    peer_id = _channel_peer_id(int(update.channel_id))
    items = tuple(_empty_item(message_id, event_type=event_type, pinned=pinned) for message_id in update.messages)
    identity = f"{event_type}:{peer_id}:{','.join(str(item['source_message_id']) for item in items)}:{int(getattr(update, 'pts', 0) or 0)}"
    return TelegramNormalizedUpdate(
        identity_key=identity,
        constructor_name=type(update).__name__,
        pts=int(getattr(update, "pts", 0) or 0) or None,
        pts_count=int(getattr(update, "pts_count", 0) or 0) or None,
        routing_peer_type="channel",
        routing_peer_id=peer_id,
        normalized_items=items,
    )


def _minimal_update_envelope(update: Any) -> TelegramNormalizedUpdate:
    name = type(update).__name__
    identity = hashlib.sha256(_json(_safe_update_identity(update)).encode()).hexdigest()
    return TelegramNormalizedUpdate(
        identity_key=f"minimal:{name}:{identity}",
        constructor_name=name,
        pts=int(getattr(update, "pts", 0) or 0) or None,
        pts_count=int(getattr(update, "pts_count", 0) or 0) or None,
    )


def _normalized_message(
    message: Any,
    *,
    event_type: str,
    sender_names: dict[tuple[str, str], str] | None = None,
    sender_bots: dict[tuple[str, str], bool] | None = None,
) -> dict[str, Any] | None:
    peer_type, peer_id = _peer_identity(getattr(message, "peer_id", None))
    if not peer_id or int(getattr(message, "id", 0) or 0) <= 0:
        return None
    action_item = _service_action_item(message, peer_type=peer_type, peer_id=peer_id)
    if action_item is not None:
        return action_item
    reply = getattr(message, "reply_to", None)
    sender_identity = _peer_identity(getattr(message, "from_id", None))
    sent_at = getattr(message, "date", None)
    return {
        "routing_peer_type": peer_type,
        "routing_peer_id": peer_id,
        "source_message_id": int(message.id),
        "event_type": event_type,
        "sender_peer_type": sender_identity[0],
        "sender_peer_id": sender_identity[1],
        "sender_name": _sender_name(message, sender_names or {}),
        "sender_is_bot": bool((sender_bots or {}).get(sender_identity, False)),
        "reply_to_message_id": _optional_int(getattr(reply, "reply_to_msg_id", None)),
        "source_top_message_id": _optional_int(getattr(reply, "reply_to_top_id", None)),
        "grouped_id": str(message.grouped_id) if getattr(message, "grouped_id", None) else None,
        "media_type": _media_type(getattr(message, "media", None)),
        "content": str(getattr(message, "message", "") or ""),
        "entities": [_entity_dict(item) for item in (getattr(message, "entities", None) or ())],
        "poll_snapshot": _poll_snapshot(getattr(message, "media", None)),
        "protected_content": bool(getattr(message, "noforwards", False)),
        "message_revision": _message_revision(message, event_type),
        "sent_at": sent_at.isoformat() if isinstance(sent_at, datetime) else None,
    }


def _service_action_item(message: Any, *, peer_type: str, peer_id: str) -> dict[str, Any] | None:
    action = getattr(message, "action", None)
    name = type(action).__name__ if action is not None else ""
    reply = getattr(message, "reply_to", None)
    if name == "MessageActionPinMessage":
        pinned_id = _optional_int(getattr(reply, "reply_to_msg_id", None))
        if not pinned_id:
            return None
        return {
            **_empty_item(pinned_id, event_type="message_pin", pinned=True),
            "routing_peer_type": peer_type,
            "routing_peer_id": peer_id,
        }
    if name not in {"MessageActionTopicCreate", "MessageActionTopicEdit"}:
        return None
    event_type = "topic_create" if name.endswith("Create") else "topic_edit"
    top_id = int(message.id) if event_type == "topic_create" else _optional_int(getattr(reply, "reply_to_top_id", None))
    return {
        **_empty_item(int(message.id), event_type=event_type, pinned=None),
        "routing_peer_type": peer_type,
        "routing_peer_id": peer_id,
        "source_top_message_id": top_id,
        "content": str(getattr(action, "title", "") or ""),
        "media_type": "topic",
        "poll_snapshot": _topic_snapshot(action),
    }


def _difference_messages_envelope(
    items: list[dict[str, Any]],
    *,
    peer_id: str,
    requested_pts: int,
    result_pts: int,
) -> TelegramNormalizedUpdate:
    clean = []
    for item in items:
        clone = dict(item)
        clone.pop("routing_peer_type", None)
        clone.pop("routing_peer_id", None)
        clean.append(clone)
    message_ids = ",".join(str(item["source_message_id"]) for item in clean)
    return TelegramNormalizedUpdate(
        identity_key=f"difference_messages:{peer_id}:{requested_pts}:{result_pts}:{message_ids}",
        constructor_name="DifferenceMessages",
        pts=result_pts or None,
        pts_count=max(1, result_pts - requested_pts) if result_pts else None,
        routing_peer_type="channel" if peer_id.startswith("-100") else "chat",
        routing_peer_id=peer_id,
        normalized_items=tuple(clean),
    )


def _empty_item(message_id: int, *, event_type: str, pinned: bool | None) -> dict[str, Any]:
    snapshot = {"pinned": pinned} if pinned is not None else {}
    return {
        "source_message_id": int(message_id),
        "event_type": event_type,
        "sender_peer_type": None,
        "sender_peer_id": None,
        "sender_name": "",
        "sender_is_bot": False,
        "reply_to_message_id": None,
        "source_top_message_id": None,
        "grouped_id": None,
        "media_type": None,
        "content": "",
        "entities": [],
        "poll_snapshot": snapshot,
        "protected_content": False,
        "message_revision": 1,
        "sent_at": None,
    }


def _peer_identity(peer: Any) -> tuple[str | None, str | None]:
    if peer is None:
        return None, None
    name = type(peer).__name__
    value = int(getattr(peer, "channel_id", 0) or getattr(peer, "chat_id", 0) or getattr(peer, "user_id", 0) or 0)
    if not value:
        return None, None
    if name == "PeerChannel":
        return "channel", _channel_peer_id(value)
    if name == "PeerChat":
        return "chat", str(-value)
    return "user", str(value)


def _sender_names(entities: Any) -> dict[tuple[str, str], str]:
    result = {}
    for entity in entities or ():
        value = int(getattr(entity, "id", 0) or 0)
        name = type(entity).__name__
        identity = (
            ("user", str(value)) if name == "User" and value
            else ("channel", _channel_peer_id(value)) if name == "Channel" and value
            else ("chat", str(-value)) if name == "Chat" and value
            else (None, None)
        )
        title = str(getattr(entity, "title", "") or "").strip()
        personal = " ".join(filter(None, (
            str(getattr(entity, "first_name", "") or "").strip(),
            str(getattr(entity, "last_name", "") or "").strip(),
        )))
        name = title or personal or str(getattr(entity, "username", "") or "").strip()
        if identity[0] and identity[1] and name:
            result[(identity[0], identity[1])] = name
    return result


def _sender_bot_flags(entities: Any) -> dict[tuple[str, str], bool]:
    return {
        ("user", str(int(entity.id))): bool(getattr(entity, "bot", False))
        for entity in entities or ()
        if type(entity).__name__ == "User" and int(getattr(entity, "id", 0) or 0)
    }


def _sender_name(message: Any, names: dict[tuple[str, str], str]) -> str:
    post_author = str(getattr(message, "post_author", "") or "").strip()
    if post_author:
        return post_author
    return names.get(_peer_identity(getattr(message, "from_id", None)), "")


def _channel_peer_id(channel_id: int) -> str:
    return str(-(1_000_000_000_000 + int(channel_id)))


def _media_type(media: Any) -> str:
    name = type(media).__name__ if media is not None else ""
    if name in {"", "MessageMediaEmpty", "MessageMediaWebPage"}:
        return "text"
    if name == "MessageMediaPhoto":
        return "photo"
    if name == "MessageMediaPoll":
        return "poll"
    if name == "MessageMediaDocument":
        return _document_media_type(getattr(media, "document", None))
    return name.removeprefix("MessageMedia").lower() or "unknown"


def _document_media_type(document: Any) -> str:
    names = {type(item).__name__ for item in (getattr(document, "attributes", None) or ())}
    mime = str(getattr(document, "mime_type", "") or "")
    if "DocumentAttributeSticker" in names:
        return "sticker"
    if "DocumentAttributeAnimated" in names:
        return "animation"
    if "DocumentAttributeAudio" in names:
        return "voice" if any(bool(getattr(item, "voice", False)) for item in document.attributes) else "audio"
    if "DocumentAttributeVideo" in names or mime.startswith("video/"):
        is_round = any(
            bool(getattr(item, "round_message", False))
            for item in document.attributes
        )
        return "video_note" if is_round else "video"
    return "document"


def _entity_dict(entity: Any) -> dict[str, Any]:
    result = {
        "type": type(entity).__name__.removeprefix("MessageEntity").lower(),
        "offset": int(getattr(entity, "offset", 0) or 0),
        "length": int(getattr(entity, "length", 0) or 0),
    }
    for key in ("url", "language", "user_id", "document_id"):
        value = getattr(entity, key, None)
        if value is not None:
            result[key] = str(value)
    return result


def _poll_snapshot(media: Any) -> dict[str, Any]:
    if type(media).__name__ != "MessageMediaPoll":
        return {}
    poll = media.poll
    results = media.results
    question = getattr(getattr(poll, "question", None), "text", "") or ""
    answer_options = [bytes(item.option) for item in (poll.answers or ())]
    correct_options = [
        bytes(item.option)
        for item in (getattr(results, "results", None) or ())
        if bool(getattr(item, "correct", False))
    ]
    return {
        "poll_id": str(poll.id),
        "question": question,
        "answers": [
            {"text": getattr(getattr(item, "text", None), "text", "") or "", "option": bytes(item.option).hex()}
            for item in (poll.answers or ())
        ],
        "closed": bool(poll.closed),
        "public_voters": bool(poll.public_voters),
        "multiple_choice": bool(poll.multiple_choice),
        "quiz": bool(poll.quiz),
        "correct_answer_indices": [
            answer_options.index(option)
            for option in correct_options
            if option in answer_options
        ],
        "total_voters": int(getattr(results, "total_voters", 0) or 0),
    }


def _topic_snapshot(action: Any) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "icon_color": getattr(action, "icon_color", None),
            "icon_emoji_id": str(getattr(action, "icon_emoji_id", "") or "") or None,
            "closed": getattr(action, "closed", None),
            "hidden": getattr(action, "hidden", None),
        }.items()
        if value is not None
    }


def _message_identity(item: dict[str, Any], *, pts: int) -> str:
    return ":".join((
        str(item["event_type"]),
        str(item["routing_peer_id"]),
        str(item["source_message_id"]),
        str(item["message_revision"]),
        str(pts),
    ))


def _message_revision(message: Any, event_type: str) -> int:
    edit_date = getattr(message, "edit_date", None)
    if event_type == "message_edit" and isinstance(edit_date, datetime):
        return max(2, _unix(edit_date))
    return 1


def _state_cursor(state: Any) -> dict[str, int]:
    return {
        "pts": int(getattr(state, "pts", 0) or 0),
        "qts": int(getattr(state, "qts", 0) or 0),
        "date": _unix(getattr(state, "date", None)),
        "seq": int(getattr(state, "seq", 0) or 0),
    }


def _channel_result_pts(result: Any) -> int:
    direct = int(getattr(result, "pts", 0) or 0)
    if direct:
        return direct
    return int(getattr(getattr(result, "dialog", None), "pts", 0) or 0)


def _channel_status(name: str, result: Any) -> str:
    if name == "ChannelDifferenceTooLong":
        return "too_long"
    if name == "ChannelDifferenceEmpty":
        return "empty"
    return "live" if bool(getattr(result, "final", True)) else "slice"


def _cursor_date(value: int) -> datetime:
    return datetime.fromtimestamp(max(0, int(value or 0)), tz=timezone.utc)


def _unix(value: Any) -> int:
    if isinstance(value, datetime):
        return int(value.timestamp())
    return int(value or 0)


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None


def _safe_update_identity(update: Any) -> dict[str, Any]:
    return {
        "constructor": type(update).__name__,
        "pts": int(getattr(update, "pts", 0) or 0),
        "pts_count": int(getattr(update, "pts_count", 0) or 0),
        "channel_id": int(getattr(update, "channel_id", 0) or 0),
    }


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


__all__ = ["fetch_channel_difference", "fetch_update_difference", "fetch_update_state"]
