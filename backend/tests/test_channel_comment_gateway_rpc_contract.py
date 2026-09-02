import pytest

from app.integrations.telegram.gateway import (
    _channel_comment_send_kwargs,
    _map_channel_comment_rpc_error,
)


pytestmark = pytest.mark.no_postgres


def test_channel_top_level_and_discussion_reply_rpc_are_mutually_exclusive() -> None:
    assert _channel_comment_send_kwargs("channel_comment_to", 91, None) == {
        "comment_to": 91,
    }
    assert _channel_comment_send_kwargs("discussion_reply_to", 91, 301) == {
        "reply_to": 301,
    }
    with pytest.raises(ValueError, match="channel_comment_rpc_identity_conflict"):
        _channel_comment_send_kwargs("channel_comment_to", 91, 301)
    with pytest.raises(ValueError, match="channel_comment_reply_identity_missing"):
        _channel_comment_send_kwargs("discussion_reply_to", 91, None)


@pytest.mark.parametrize(
    ("exception_name", "failure_code"),
    [
        ("UserNotParticipantError", "discussion_membership_required"),
        ("ChatWriteForbiddenError", "discussion_send_forbidden"),
        ("ChannelPrivateError", "discussion_access_rejected_for_account"),
        ("UserBannedInChannelError", "account_banned_in_discussion"),
        ("MessageIdInvalidError", "source_comment_identity_reprobe_required"),
    ],
)
def test_comment_rpc_class_maps_to_authoritative_pre_mutation_failure(
    exception_name: str,
    failure_code: str,
) -> None:
    error_type = type(exception_name, (Exception,), {})
    result = _map_channel_comment_rpc_error(error_type())

    assert result.failure_type == failure_code
    assert result.remote_mutation_started is False


def test_unclassified_rpc_error_stays_unknown() -> None:
    result = _map_channel_comment_rpc_error(RuntimeError("opaque failure"))

    assert result.failure_type == "comment_remote_result_unknown"
    assert result.remote_mutation_started is None
