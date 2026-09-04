"""Public view access is independent of channel membership and posting rights."""


def public_channel_view(task_type: str, target) -> bool:
    return bool(
        task_type == "channel_view"
        and target is not None
        and target.target_type == "channel"
        and str(target.username or "").strip().lstrip("@")
    )
