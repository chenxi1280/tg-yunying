"""One logical reaction source identity for allocation and execution lineage."""


def reaction_source_identity(message):
    if message.grouped_id:
        return f"channel:{message.channel_target_id}:album:{message.grouped_id}"
    revision = message.current_source_revision_id or "unversioned"
    return f"channel:{message.channel_target_id}:message:{message.message_id}:revision:{revision}"
