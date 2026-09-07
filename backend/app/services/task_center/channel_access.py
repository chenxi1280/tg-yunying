"""Public view access requires channel membership consistent with all channel interactions."""


def public_channel_view(task_type: str, target) -> bool:
    return False
