from types import SimpleNamespace

import pytest

from app.telegram_owner import attempts, request_context
from app.services.task_center import dispatcher
from app.services.task_center.telegram_worker_exit_reconcile import preview_worker_exits
from test_telegram_worker_exit_reconcile import _legacy, _spec, _session

pytestmark = pytest.mark.no_postgres


def test_attempt_records_owner_and_preserves_it_in_result_merge(monkeypatch):
    monkeypatch.setattr(attempts, 'get_settings', lambda: SimpleNamespace(telegram_owner_mode='client'))
    monkeypatch.setattr(attempts.OwnerGateway, 'call', lambda *_: {'instance_id': 'owner-one', 'release_sha': 'release'})
    attempt = SimpleNamespace(id='attempt-one', result_snapshot={'existing': True})
    token = request_context.bind_identity('')
    try:
        attempts.prepare_attempt(attempt)
        assert request_context.current_identity() == 'telegram-gateway:attempt-one'
        assert request_context.expected_instance() == 'owner-one'
        assert attempt.result_snapshot['transport_owner_instance_id'] == 'owner-one'
        assert attempt.result_snapshot['existing']
        action = SimpleNamespace(result={'transport_owner_instance_id': 'wrong'})
        merged = dispatcher._merge_attempt_result_snapshot(attempt, action, remote_fact_id='fact')
        assert merged['transport_owner_instance_id'] == 'owner-one'
        assert merged['remote_fact_id'] == 'fact'
    finally:
        request_context.reset_identity(token)


def test_business_worker_exit_cannot_acknowledge_separate_owner():
    with _session() as session:
        _, attempt = _legacy(session)
        attempt.result_snapshot = {**attempt.result_snapshot, 'transport_owner_kind': 'telegram_owner'}
        session.commit()
        with pytest.raises(ValueError, match='owner'):
            preview_worker_exits(session, _spec(attempt))
