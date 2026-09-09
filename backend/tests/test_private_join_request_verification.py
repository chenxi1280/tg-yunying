"""Use real Telethon request/error types with synthetic private challenges."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon import functions, types
from telethon.errors import InviteRequestSentError

from app.integrations.telegram import join_request_verification as protocol
from app.integrations.telegram.channel_membership_gateway import ensure_membership
from app.integrations.telegram.contracts import ChannelMembershipResult, OperationResult
from app.models import Action, ExecutionAttempt
from app.services.task_center import dispatcher


pytestmark = [pytest.mark.no_postgres, pytest.mark.anyio]


def challenge_fixture():
    now = datetime.now(timezone.utc)
    bot = SimpleNamespace(id=123, bot=True)
    entity = SimpleNamespace(id=456, title="合成测试群", megagroup=True, join_request=True)
    button = types.KeyboardButtonCallback(text="42", data=b"synthetic-callback")
    message = SimpleNamespace(id=11, sender_id=123, is_private=True, out=False, date=now,
        raw_text="来自『合成测试群』的申请入群验证\n数学问题：20+22=?\n请在60秒内完成回答",
        reply_markup=SimpleNamespace(rows=[SimpleNamespace(buttons=[button])]))
    return now, bot, entity, message


def fake_client(bot, entity, message):
    client = AsyncMock()
    client.get_participants.return_value = [bot]
    client.get_entity.return_value = entity
    async def messages(_peer, **kwargs):
        return message if "ids" in kwargs else [message]
    client.get_messages.side_effect = messages
    return client


@pytest.mark.parametrize("expression,answer", [(('20','+','22'),'42'), (('3','-','9'),'-6'),
    (('6','×','7'),'42'), (('84','÷','2'),'42'), (('1','/','0'),''), (('5','/','2'),'')])
async def test_deterministic_integer_arithmetic(expression, answer):
    assert protocol.arithmetic_answer(expression) == answer


@pytest.mark.parametrize("invalid", ['wrong_sender','other_group','old','expired','outgoing','group_chat','duplicate','url'])
async def test_invalid_challenges_cannot_supply_a_callback(invalid):
    now, bot, entity, message = challenge_fixture()
    if invalid == 'wrong_sender': message.sender_id = 999
    if invalid == 'other_group': message.raw_text = message.raw_text.replace('合成测试群','其他群')
    if invalid == 'old': message.date = now - timedelta(seconds=2)
    if invalid == 'expired': message.date = now - timedelta(seconds=61)
    if invalid == 'outgoing': message.out = True
    if invalid == 'group_chat': message.is_private = False
    if invalid == 'duplicate': message.reply_markup.rows[0].buttons *= 2
    if invalid == 'url': message.reply_markup.rows[0].buttons = [types.KeyboardButtonUrl(text='42',url='https://example.invalid')]
    assert protocol.parse_challenge(message, bot=bot, group_title=entity.title, requested_at=now) is None


async def test_fresh_answer_requires_callback_then_separate_membership_observation():
    now, bot, entity, message = challenge_fixture()
    client = fake_client(bot, entity, message)
    baseline = protocol.JoinRequestBaseline(entity, (bot,), (10,))
    result = await protocol.resolve_join_request(client, baseline, requested_at=now)
    requests = [call.args[0] for call in client.await_args_list]
    assert isinstance(requests[0], functions.messages.GetBotCallbackAnswerRequest)
    assert isinstance(requests[1], functions.channels.GetParticipantRequest)
    assert result['membership_observed'] and result['callback_response_observed']
    assert result['bot_peer_id'] == 123 and result['challenge_message_id'] == 11
    assert 'synthetic-callback' not in str(result)


async def test_callback_ack_without_membership_is_not_success(monkeypatch):
    now, bot, entity, message = challenge_fixture()
    client = fake_client(bot, entity, message)
    monkeypatch.setattr(protocol, '_observe_member', AsyncMock(return_value=False))
    result = await protocol.resolve_join_request(client, protocol.JoinRequestBaseline(entity,(bot,),(10,)),requested_at=now)
    assert result['callback_response_observed'] and not result['membership_observed']
    assert result['verification_status'] == 'approval_not_observed'
    client.assert_awaited_once()


async def test_callback_response_lost_is_never_replayed():
    now, bot, entity, message = challenge_fixture()
    client = fake_client(bot, entity, message)
    client.side_effect = TimeoutError('response lost')
    result = await protocol.resolve_join_request(client,protocol.JoinRequestBaseline(entity,(bot,),(10,)),requested_at=now)
    assert result['callback_started'] and not result['callback_response_observed']
    assert result['verification_status'] == 'callback_result_unknown'
    client.assert_awaited_once()


async def test_bot_removed_from_admins_before_callback_is_rejected():
    now, bot, entity, message = challenge_fixture()
    client = fake_client(bot, entity, message)
    client.get_participants.return_value = []
    result = await protocol.resolve_join_request(client,protocol.JoinRequestBaseline(entity,(bot,),(10,)),requested_at=now)
    assert result['verification_status'] == 'challenge_bot_no_longer_admin'
    client.assert_not_awaited()


async def test_pre_request_cursor_excludes_old_message():
    now, bot, entity, message = challenge_fixture()
    baseline = protocol.JoinRequestBaseline(entity,(bot,),(11,))
    assert protocol._challenge_candidates(baseline, [[message]], requested_at=now) == []


@pytest.mark.parametrize('invite_hash',['','synthetic-invite'])
async def test_invite_request_is_explicit_pending_with_no_automatic_reply_when_disabled(invite_hash):
    _now, bot, entity, message = challenge_fixture()
    client = fake_client(bot, entity, message)
    client.side_effect = InviteRequestSentError(request=None)
    result = await ensure_membership(client, 'synthetic', invite_hash=invite_hash, verify_join_request=False,
                                     map_error=lambda error: OperationResult(False,detail=type(error).__name__))
    assert not result.ok and result.membership_status == 'pending_approval'
    assert result.failure_type == 'join_request_pending' and result.remote_mutation_started is True
    assert result.join_request_evidence['join_request_submitted']
    client.get_participants.assert_not_awaited()
    client.assert_awaited_once()


async def test_enabled_membership_path_captures_cursor_before_join_and_verifies_new_challenge():
    _now, bot, entity, message = challenge_fixture()
    client = fake_client(bot, entity, message)
    async def messages(_peer, **kwargs):
        if kwargs.get('limit') == 1:
            return [SimpleNamespace(id=10)]
        return message if 'ids' in kwargs else [message]
    async def rpc(request):
        if isinstance(request, functions.channels.JoinChannelRequest):
            raise InviteRequestSentError(request=request)
        return object()
    client.get_messages.side_effect = messages
    client.side_effect = rpc
    result = await ensure_membership(client,'synthetic',invite_hash='',verify_join_request=True,
                                     map_error=lambda error: OperationResult(False,detail=type(error).__name__))
    assert result.ok and result.membership_status == 'joined'
    assert result.join_request_evidence['membership_observed']
    assert [type(call.args[0]) for call in client.await_args_list] == [
        functions.channels.JoinChannelRequest, functions.messages.GetBotCallbackAnswerRequest,
        functions.channels.GetParticipantRequest]


async def test_ordinary_group_does_not_read_private_chats():
    _now, bot, entity, message = challenge_fixture()
    entity.join_request = False
    client = fake_client(bot, entity, message)
    result = await ensure_membership(client,'synthetic',invite_hash='',verify_join_request=True,
                                     map_error=lambda error: OperationResult(False,detail=type(error).__name__))
    assert result.ok
    client.get_participants.assert_not_awaited()
    client.get_messages.assert_not_awaited()


async def test_pending_dispatch_holds_original_action_instead_of_rejoining_or_claiming_joined(monkeypatch):
    action = Action(id='test',tenant_id=1,task_id='test',task_type='group_ai_chat',action_type='ensure_target_membership',status='executing')
    attempt = ExecutionAttempt(id='attempt',tenant_id=1,action_id='test')
    context = SimpleNamespace(action=action,attempt=attempt)
    result = ChannelMembershipResult(False,'待审批','join_request_pending','已提交申请','pending_approval',remote_mutation_started=True)
    monkeypatch.setattr(dispatcher,'_try_admin_approve_join_request',lambda *_: OperationResult(False))
    monkeypatch.setattr(dispatcher,'_release_runtime_resources',lambda *_: None)
    monkeypatch.setattr(dispatcher,'_clear_group_bot_admission_window',lambda *_: None)
    finish = MagicMock()
    monkeypatch.setattr(dispatcher,'_finish_execution_attempt',finish)
    assert dispatcher._hold_pending_join_request(context,result)
    assert action.status == 'unknown_after_send'
    assert action.result['membership_status'] == 'pending_approval' and not action.result['success']
    assert action.result['auto_check'] == '等待审批'
    assert finish.call_args.kwargs['remote_mutation_started'] is True


async def test_pending_keeps_existing_authorized_admin_approval_path(monkeypatch):
    action = SimpleNamespace(result={})
    ctx = SimpleNamespace(session=None,action=action,account=None,payload=None,attempt=None)
    result = ChannelMembershipResult(False,'待审批','join_request_pending','已提交入群申请','pending_approval',remote_mutation_started=True)
    monkeypatch.setattr(dispatcher,'_try_admin_approve_join_request',lambda *_: OperationResult(True,detail='permission_observed'))
    marked, applied = MagicMock(), MagicMock()
    monkeypatch.setattr(dispatcher,'_mark_membership_joined',marked)
    monkeypatch.setattr(dispatcher,'_apply_operation_result',applied)
    assert dispatcher._complete_pending_join_admin_approval(ctx,result)
    marked.assert_called_once()
    assert action.result['membership_status'] == 'joined'
    applied.assert_called_once()


async def test_unknown_callback_does_not_start_admin_approval(monkeypatch):
    approve = MagicMock()
    monkeypatch.setattr(dispatcher,'_try_admin_approve_join_request',approve)
    result = ChannelMembershipResult(False,'待审批','join_request_pending','pending','pending_approval',remote_mutation_started=None)
    assert not dispatcher._complete_pending_join_admin_approval(None,result)
    approve.assert_not_called()
