from __future__ import annotations


def telethon_send_target(peer_id: str, *, group_id: int = 0) -> int | str:
    if not peer_id.lstrip("-").isdigit():
        return peer_id
    raw_peer_id = int(peer_id)
    if group_id and raw_peer_id > 0:
        return -raw_peer_id
    return raw_peer_id


async def resolve_telethon_target(client, peer_id: str, *, group_id: int = 0):
    from telethon import utils

    target = telethon_send_target(peer_id, group_id=group_id)
    try:
        entity = await client.get_entity(target)
        return getattr(entity, "migrated_to", None) or entity
    except Exception:
        expected_peer_id = str(target)
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            resolved_entity = getattr(entity, "migrated_to", None) or entity
            if (
                str(utils.get_peer_id(entity)) == expected_peer_id
                or str(utils.get_peer_id(resolved_entity)) == expected_peer_id
            ):
                return resolved_entity
        raise
