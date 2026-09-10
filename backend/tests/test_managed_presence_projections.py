from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from app.models import Action, Task, TaskDayLedger, TgGroup
from app.services.task_center.engagement_natural_opportunity import _presence_evidence
from app.services.task_center.managed_presence_queries import _managed_actions
from tests.test_engagement_natural_opportunity import DAY_START, _session

pytestmark = pytest.mark.no_postgres
LARGE_BODY = 'irrelevant-payload' * 1000


def _action(identity, *, status='pending', group_id=21, tenant=1, minutes=1, visible=None):
    return Action(id=identity, tenant_id=tenant, task_id='different-task',
        task_type='group_ai_chat', action_type='send_message', status=status,
        scheduled_at=DAY_START + timedelta(minutes=minutes),
        payload={'group_id': group_id, 'message_text': LARGE_BODY},
        result={'visibility_status': visible, 'unneeded': LARGE_BODY})


def test_presence_counts_preserve_cross_task_group_scope_and_statuses():
    with _session() as session:
        rows = [_action('visible', status='success', visible='visible_confirmed'),
                _action('unproven-success', status='success'), _action('unknown', status='unknown_after_send'),
                _action('pending'), _action('claiming', status='claiming'),
                _action('skipped', status='skipped'), _action('failed', status='failed'),
                _action('cancelled', status='cancelled'), _action('other-group', group_id=22),
                _action('other-tenant', tenant=2), _action('before-day', minutes=-1),
                _action('at-deadline', minutes=1440)]
        session.add_all(rows)
        session.commit()
        task, ledger, group = _scope(session)
        policy = SimpleNamespace(absolute_daily_authored_cap=20, bootstrap_allowance=2,
            managed_to_external_ratio_bps=10000, max_consecutive_system_turns=2, revision=1)
        evidence = _presence_evidence(session, task, ledger, group=group, policy=policy)
        assert evidence['visible_managed_authored_count'] == 1
        assert evidence['planned_managed_authored_count'] == 4
        assert evidence['trailing_managed_turn_count'] == 5
        assert evidence['remaining_capacity'] == 0
        assert evidence['external_human_turn_count'] == 0


def _scope(session):
    return session.get(Task, 'group-presence'), session.get(TaskDayLedger, 'group-presence-day'), session.get(TgGroup, 21)


@pytest.mark.parametrize('group_id', [21, 21.9, '21', '021', ' 21 ', None, False])
def test_group_json_scalar_preserves_original_python_conversion(group_id):
    with _session() as session:
        session.add(_action('scalar', group_id=group_id))
        session.commit()
        task, ledger, group = _scope(session)
        rows = _managed_actions(session, task, ledger, group=group)
        assert len(rows) == int(int(group_id or 0) == group.id)


@pytest.mark.parametrize('group_id', ['malformed', {}, [21]])
def test_invalid_group_values_are_not_silently_filtered(group_id):
    with _session() as session:
        session.add(_action('invalid', group_id=group_id))
        session.commit()
        task, ledger, group = _scope(session)
        if group_id == {}:
            assert _managed_actions(session, task, ledger, group=group) == []
        else:
            with pytest.raises((ValueError, TypeError)):
                _managed_actions(session, task, ledger, group=group)


def test_query_projects_scalars_and_does_not_hydrate_action_entities():
    with _session() as session:
        session.add(_action('projection'))
        session.commit()
        session.expunge_all()
        task, ledger, group = _scope(session)
        captured = []
        def capture(_conn, _cursor, _statement, _params, context, _many):
            captured.append(tuple(context.compiled.statement.selected_columns.keys()))
        event.listen(session.bind, 'before_cursor_execute', capture)
        try:
            rows = _managed_actions(session, task, ledger, group=group)
        finally:
            event.remove(session.bind, 'before_cursor_execute', capture)
        assert captured == [('group_value', 'status', 'visibility_status', 'executed_at', 'scheduled_at')]
        assert len(rows) == 1
        assert not any(isinstance(obj, Action) for obj in session.identity_map.values())


def test_executed_time_has_priority_and_start_is_inclusive():
    with _session() as session:
        before = _action('executed-before', minutes=1)
        before.executed_at = DAY_START - timedelta(seconds=1)
        now = _action('executed-start', minutes=1440)
        now.executed_at = DAY_START
        session.add_all([before, now])
        session.commit()
        task, ledger, group = _scope(session)
        rows = _managed_actions(session, task, ledger, group=group)
        assert len(rows) == 1
        assert rows[0].executed_at == DAY_START.replace(tzinfo=None)


def test_external_and_unowned_times_preserve_trailing_counts():
    from app.models import ContextTurn, ConversationEvent, UnownedOutboundActivityObservation
    with _session() as session:
        human_at = DAY_START + timedelta(minutes=2)
        session.add(ConversationEvent(id='human', tenant_id=1, surface='group_ai_chat',
            canonical_peer_id='-10021', remote_message_id='1', author_class='external_human',
            content_hash='test', source_context_message_id=1, sent_at=human_at))
        session.flush()
        session.add(ContextTurn(tenant_id=1, surface='group_ai_chat', canonical_peer_id='-10021',
            turn_family_key='human', anchor_event_id='human', state='closed',
            first_event_at=human_at, last_event_at=human_at, closed_at=human_at))
        session.add(UnownedOutboundActivityObservation(tenant_id=1, account_id=1,
            activity_class='authored_message', canonical_peer_id='-10021', remote_identity='2',
            activity_identity_hash='test', observed_at=human_at+timedelta(minutes=1)))
        session.add(_action('before-human', minutes=1))
        session.commit()
        task, ledger, group = _scope(session)
        policy = SimpleNamespace(absolute_daily_authored_cap=20, bootstrap_allowance=2,
            managed_to_external_ratio_bps=10000, max_consecutive_system_turns=2, revision=1)
        evidence = _presence_evidence(session, task, ledger, group=group, policy=policy)
        assert evidence['external_human_turn_count'] == 1
        assert evidence['trailing_managed_turn_count'] == 1
        assert evidence['unowned_managed_authored_count'] == 1
