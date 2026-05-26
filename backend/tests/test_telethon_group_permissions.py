from types import SimpleNamespace

from app.integrations.telegram.gateway import _can_send_text_in_group


def test_regular_supergroup_member_can_send_when_default_text_is_not_banned():
    target = SimpleNamespace(
        default_banned_rights=SimpleNamespace(send_messages=False, send_plain=False),
    )
    permissions = SimpleNamespace(
        is_admin=False,
        is_creator=False,
        post_messages=False,
        participant=SimpleNamespace(),
    )

    assert _can_send_text_in_group(target, permissions) is True


def test_supergroup_member_cannot_send_when_default_text_is_banned():
    target = SimpleNamespace(
        default_banned_rights=SimpleNamespace(send_messages=True, send_plain=False),
    )
    permissions = SimpleNamespace(
        is_admin=False,
        is_creator=False,
        post_messages=False,
        participant=SimpleNamespace(),
    )

    assert _can_send_text_in_group(target, permissions) is False
