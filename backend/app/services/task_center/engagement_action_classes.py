from __future__ import annotations


ACTION_CLASS_BY_TYPE = {
    "send_message": "authored_message",
    "post_comment": "authored_comment",
    "like_message": "reaction",
    "view_message": "view",
}


def action_class_for_type(action_type: str) -> str:
    return ACTION_CLASS_BY_TYPE.get(action_type, "")


__all__ = ["ACTION_CLASS_BY_TYPE", "action_class_for_type"]
