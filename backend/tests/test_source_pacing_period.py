from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.task_center.source_pacing_admission import _source_period


pytestmark = pytest.mark.no_postgres
NOW = datetime(2026, 8, 19, 0, 0)


class LedgerSession:
    def __init__(self, ledger) -> None:
        self.ledger = ledger

    def get(self, _model, _key):
        return self.ledger


def test_view_source_period_uses_frozen_owner_period_key() -> None:
    ledger = SimpleNamespace(
        id="ledger-1",
        period_start_at=NOW,
        deadline_at=NOW + timedelta(days=1),
    )
    owner = SimpleNamespace(
        task_day_ledger_id=ledger.id,
        pacing_period_key="ledger-1:message:42",
    )

    _start, _deadline, period_key = _source_period(
        LedgerSession(ledger), owner, "view"
    )

    assert period_key == "ledger-1:message:42"


def test_ai_source_period_keeps_ledger_fallback() -> None:
    ledger = SimpleNamespace(
        id="ledger-1",
        period_start_at=NOW,
        deadline_at=NOW + timedelta(days=1),
    )
    owner = SimpleNamespace(task_day_ledger_id=ledger.id, pacing_period_key=None)

    _start, _deadline, period_key = _source_period(
        LedgerSession(ledger), owner, "ai_send"
    )

    assert period_key == ledger.id


def test_ledger_source_period_converts_naive_utc_storage_to_beijing_wall() -> None:
    ledger = SimpleNamespace(
        id="ledger-utc",
        period_start_at=datetime(2026, 8, 27, 16, 0),
        deadline_at=datetime(2026, 8, 28, 16, 0),
    )
    owner = SimpleNamespace(
        task_day_ledger_id=ledger.id,
        pacing_period_key=None,
    )

    start, deadline, _period_key = _source_period(
        LedgerSession(ledger), owner, "view"
    )

    assert start == datetime(2026, 8, 28, 0, 0)
    assert deadline == datetime(2026, 8, 29, 0, 0)
