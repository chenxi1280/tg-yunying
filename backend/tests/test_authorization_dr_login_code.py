from datetime import datetime, timedelta, timezone

import pytest

from app.integrations.telegram import VerificationCodeSnapshot
from app.services.authorization_dr.contracts import AuthorizationDrError
from app.services.authorization_dr.login_code import bind_login_code


pytestmark = pytest.mark.no_postgres


def _snapshot(message_id: str, received_at: datetime) -> VerificationCodeSnapshot:
    return VerificationCodeSnapshot(
        code="12345",
        raw_hint="official",
        expires_at=received_at + timedelta(minutes=5),
        message_id=message_id,
        received_at=received_at,
    )


def test_binds_unique_code_within_three_second_clock_skew() -> None:
    challenge = datetime(2026, 8, 22, 4, 18, 52, 13558, tzinfo=timezone(timedelta(hours=8)))
    received = datetime(2026, 8, 21, 20, 18, 51, tzinfo=timezone.utc)

    result = bind_login_code([_snapshot("233", received)], challenge_sent_at=challenge)

    assert result is not None
    assert result.message_id == "233"


def test_rejects_code_older_than_clock_skew_window() -> None:
    challenge = datetime(2026, 8, 22, 4, 18, 52, tzinfo=timezone(timedelta(hours=8)))
    received = challenge - timedelta(seconds=4)

    assert bind_login_code([_snapshot("old", received)], challenge_sent_at=challenge) is None


def test_multiple_codes_inside_skew_window_remain_ambiguous() -> None:
    challenge = datetime(2026, 8, 22, 4, 18, 52, tzinfo=timezone(timedelta(hours=8)))
    snapshots = [
        _snapshot("first", challenge - timedelta(seconds=1)),
        _snapshot("second", challenge + timedelta(seconds=1)),
    ]

    with pytest.raises(AuthorizationDrError, match="Multiple login codes"):
        bind_login_code(snapshots, challenge_sent_at=challenge)
