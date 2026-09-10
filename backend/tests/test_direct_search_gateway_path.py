import json
from uuid import uuid4

import pytest

from app.config import Settings
from app.integrations.telegram.gateway import TelethonTelegramGateway, _search_join_client_metadata
from app.search_transport import direct_result_proven
from app.security import encrypt_session
from app.services.task_center.search_join_facts import has_complete_pure_click_fact
from app.services.task_center.dispatcher import _normalize_pure_search_click_result
from app.services.task_center.payloads import SearchJoinPayload
from app.telegram_owner.request_context import bind_identity, reset_identity
from test_direct_search_transport import transport
from test_search_join_group_gateway import (
    FakeButton, FakeMessage, FakeSearchJoinClient, _payload, _pure_click_protocol_profile,
)
from test_telegram_owner_client_cache import raw_session
from test_telethon_lifecycle import reset_lifecycle_state

pytestmark = pytest.mark.no_postgres


class ConnectedSearchClient(FakeSearchJoinClient):
    def __init__(self):
        category = FakeMessage(101, [[FakeButton('👥', data=b'group-category')]])
        target = FakeMessage(102, [[FakeButton('目标群', url='https://t.me/target_group')]])
        super().__init__([FakeMessage(100, []), category, target])
        self.connected = False
        self.disconnects = 0

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False
        self.disconnects += 1

    def is_connected(self):
        return self.connected

    async def is_user_authorized(self):
        return True


@pytest.fixture(autouse=True)
def isolated_lifecycle():
    reset_lifecycle_state()
    yield
    reset_lifecycle_state()


@pytest.fixture
def gateway_path(transport, monkeypatch):
    _, credentials, declaration = transport
    settings = Settings(telegram_owner_mode='server', telegram_direct_egress_region='sv',
                        telegram_direct_egress_ip='8.8.8.8')
    monkeypatch.setattr('app.integrations.telegram.direct_search.probe_direct_egress', lambda _: '8.8.8.8')
    gateway = TelethonTelegramGateway(settings)
    created = []

    def create(*args):
        client = ConnectedSearchClient()
        created.append(client)
        return client

    monkeypatch.setattr(gateway._lifecycle, 'new_client', create)
    payload = {**_payload(bot_username='jisou', search_execution_mode='click_only',
                         approved_protocol_profile=_pure_click_protocol_profile()), **declaration,
               'authorization_id': credentials.authorization_id, 'session_role': 'primary'}
    payload = SearchJoinPayload.model_validate(payload).model_dump(mode='json')
    return gateway, credentials, payload, created


def test_public_search_gateway_reuses_owner_client_and_produces_complete_click(gateway_path):
    gateway, credentials, payload, created = gateway_path
    session = raw_session()
    owner_metadata = {'device_model': 'existing-owner-device', 'client_identity_key': 'owner-selected'}
    existing = gateway._run(gateway._get_or_create_client(credentials, session, owner_metadata))
    token = bind_identity('test-search', str(uuid4()))
    try:
        result = gateway.execute_search_join(10, payload, encrypt_session(session), credentials, 'keyword')
    finally:
        reset_identity(token)
    result = _normalize_pure_search_click_result(SearchJoinPayload.model_validate(payload), result)
    assert result.get('success') is True, result
    assert direct_result_proven(result) and has_complete_pure_click_fact(result), json.dumps(result, ensure_ascii=False)
    assert len(created) == 1 and created[0] is existing
    assert existing.disconnects == 0 and existing.connected
    assert existing.joined == [] and existing.imported_invites == []
    assert existing.sent == [('jisou', '/start'), ('jisou', 'keyword')]


def test_new_owner_search_creates_one_client_without_search_metadata(gateway_path):
    gateway, credentials, payload, created = gateway_path
    token = bind_identity('test-search', str(uuid4()))
    try:
        result = gateway.execute_search_join(10, payload, encrypt_session(raw_session()), credentials, 'keyword')
    finally:
        reset_identity(token)
    result = _normalize_pure_search_click_result(SearchJoinPayload.model_validate(payload), result)
    assert result.get('success') is True, result
    assert direct_result_proven(result) and has_complete_pure_click_fact(result), json.dumps(result, ensure_ascii=False)
    assert len(created) == 1 and created[0].disconnects == 0


def test_rank_client_reuses_existing_owner_identity(gateway_path):
    gateway, credentials, payload, created = gateway_path
    session = raw_session()
    existing = gateway._run(gateway._get_or_create_client(credentials, session, {'device_model': 'owner'}))
    client = gateway._run(gateway._rank_deboost_client(encrypt_session(session), credentials, payload))
    assert client is existing and len(created) == 1
    assert client.disconnects == 0


def test_legacy_search_metadata_does_not_gain_direct_acceptance():
    with pytest.raises(ValueError, match='metadata incomplete'):
        _search_join_client_metadata({'client_metadata': {}})


def test_public_gateway_verification_solver_continues_to_pure_click(gateway_path):
    from test_search_join_group_gateway import _verification_image_page, _verification_decision

    gateway, credentials, payload, created = gateway_path
    session = raw_session()
    client = gateway._run(gateway._get_or_create_client(credentials, session))
    verification = _verification_image_page(digit_answers=['7', '8', '9', '10', '11', '12', '13', '14'])
    client.responses = [FakeMessage(100, []), verification]
    client.edits = [FakeMessage(102, [[FakeButton('目标群', url='https://t.me/target_group')]])]
    requests = []

    def solver(request):
        requests.append(request)
        return _verification_decision('9', 0.95)

    token = bind_identity('test-search', str(uuid4()))
    try:
        result = gateway.execute_search_join(10, payload, encrypt_session(session), credentials,
                                             'keyword', image_verification_solver=solver)
    finally:
        reset_identity(token)
    result = _normalize_pure_search_click_result(SearchJoinPayload.model_validate(payload), result)
    assert result.get('success') is True, result
    assert has_complete_pure_click_fact(result) and direct_result_proven(result)
    assert len(requests) == 1 and requests[0].image_bytes == client.download_media_result
    assert verification.clicked == [(0, 2)]
    assert len(created) == 1 and client.disconnects == 0 and client.joined == []


@pytest.mark.parametrize('change', ['metadata', 'policy'])
def test_public_gateway_rejects_unapproved_identity_before_client_creation(gateway_path, change):
    gateway, credentials, payload, created = gateway_path
    if change == 'metadata':
        payload = {**payload, 'client_metadata': {'device_model': 'search-override'}}
    else:
        payload = {**payload, 'runtime_environment': {**payload['runtime_environment'],
                                                     'client_metadata_policy': 'legacy'}}
    token = bind_identity('test-search', str(uuid4()))
    try:
        result = gateway.execute_search_join(10, payload, encrypt_session(raw_session()), credentials, 'keyword')
    finally:
        reset_identity(token)
    assert result['success'] is False and result['remote_mutation_started'] is False
    assert created == []
