from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.task_center import dispatcher


@pytest.mark.no_postgres
def test_callback_unknown_result_maps_action_to_unknown_after_send() -> None:
    result = {
        "success": False,
        "error_code": "verification_callback_result_unknown",
        "callback_mutation_started": True,
        "challenge_fingerprint_hash": "a" * 64,
    }

    assert dispatcher._search_join_action_status(result) == "unknown_after_send"


class _ResultSession:
    def scalars(self, _statement):
        return iter(
            (
                {
                    "error_code": "verification_callback_result_unknown",
                    "bot_username": "jisou",
                    "challenge_fingerprint_hash": "a" * 64,
                },
                {
                    "error_code": "verification_callback_result_unknown",
                    "bot_username": "other-bot",
                    "challenge_fingerprint_hash": "b" * 64,
                },
                {
                    "error_code": "search_join_execution_failed",
                    "bot_username": "jisou",
                    "challenge_fingerprint_hash": "c" * 64,
                },
            )
        )


@pytest.mark.no_postgres
def test_only_same_bot_callback_unknown_fingerprints_are_blocked() -> None:
    action = SimpleNamespace(
        id="current-action",
        tenant_id=1,
        account_id=7,
    )

    fingerprints = dispatcher._search_join_callback_unknown_fingerprints(
        _ResultSession(),
        action,
        "@Jisou",
    )

    assert fingerprints == frozenset({"a" * 64})
