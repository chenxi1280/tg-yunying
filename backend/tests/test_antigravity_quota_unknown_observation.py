import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.models import AiProvider
from app.services.antigravity_provider_client import (
    AntigravityProviderClient, AntigravityProviderResultUnknown,
)
from app.services.task_center.ai_provider_candidate_runtime import (
    ProviderCandidatePolicy, ordered_route_providers, provider_draft_failure,
)
from app.services.task_center.ai_structured_provider_runtime import (
    StructuredProviderRequest, structured_failure_outcome,
)
from tests.test_antigravity_provider_runtime import credentials
from tests.test_engagement_runtime_resources import _session


pytestmark = pytest.mark.no_postgres
QUOTA_CODE = "antigravity_quota_limited"
UNKNOWN_CODE = "antigravity_provider_result_unknown"


def _provider(provider_id=8):
    return AiProvider(id=provider_id, provider_name="受测模型",
        model_name="gemini-3.6-flash-medium", base_url="http://fixture.invalid",
        api_key_ciphertext="test-cipher", health_status="健康")


def _failure(session, provider, *, lane, error, commit=False):
    if lane == "draft":
        policy = ProviderCandidatePolicy(model_name=provider.model_name,
            required_model_family="", allow_quota_rotation=False, purpose="group_ai_chat",
            close_transaction_before_external=commit, route_provider_ids=(8, 7))
        return provider_draft_failure(session, provider, error=error, policy=policy, has_more=True)
    request = StructuredProviderRequest(system_prompt="s", user_prompt="u",
        config={"_ai_provider_route_set_id": "existing-route", "_close_db_transaction_before_ai": commit},
        temperature=0.7, max_tokens=128, count=1, purpose="comment_realize_general",
        model_name=provider.model_name, stage="primary", required_model_family="")
    return structured_failure_outcome(session, provider, request=request, error=error, has_more=True)


@pytest.mark.parametrize("state", ("started", "unknown"))
def test_202_payload_preserves_quota_code_and_unknown_type(state):
    def transport(_request, **_kwargs):
        return json.dumps({"state": state, "error_code": QUOTA_CODE}).encode()

    with pytest.raises(AntigravityProviderResultUnknown, match=QUOTA_CODE):
        AntigravityProviderClient(http_transport=transport).generate(credentials(),
            request_id="original-request", system_prompt="s", user_prompt="u",
            json_schema={"type": "object"}, timeout=15)


@pytest.mark.parametrize("lane", ("draft", "structured"))
def test_quota_unknown_stops_current_request_and_marks_only_that_provider(lane):
    provider = _provider()
    error = AntigravityProviderResultUnknown(QUOTA_CODE)

    outcome = _failure(SimpleNamespace(), provider, lane=lane, error=error)

    assert outcome.error is error
    assert not outcome.continue_candidates and not outcome.route_retryable
    assert provider.health_status == "异常"
    assert QUOTA_CODE in provider.last_error


@pytest.mark.parametrize("lane", ("draft", "structured"))
def test_ordinary_unknown_does_not_invent_quota_failure(lane):
    provider = _provider()
    error = AntigravityProviderResultUnknown(UNKNOWN_CODE)

    outcome = _failure(SimpleNamespace(), provider, lane=lane, error=error)

    assert outcome.error is error
    assert not outcome.continue_candidates and not outcome.route_retryable
    assert provider.health_status == "健康"
    assert provider.last_check_at is None


@pytest.mark.parametrize("lane", ("draft", "structured"))
def test_persisted_quota_observation_only_changes_future_independent_route_selection(lane):
    with _session() as session:
        first, second = _provider(), _provider(7)
        second.model_name = "gemini-3.1-pro-low"
        session.add_all([first, second])
        session.commit()

        outcome = _failure(session, first, lane=lane,
            error=AntigravityProviderResultUnknown(QUOTA_CODE), commit=True)
        session.expire_all()

        assert not outcome.continue_candidates
        assert session.get(AiProvider, 8).health_status == "异常"
        assert session.get(AiProvider, 7).health_status == "健康"
        assert [row.id for row in ordered_route_providers(session, (8, 7))] == [7]
        assert len(list(session.scalars(select(AiProvider)))) == 2


@pytest.mark.parametrize("lane", ("draft", "structured"))
def test_quota_health_commit_failure_cannot_replace_original_unknown(lane):
    database_error = OperationalError("health update", {}, RuntimeError("unavailable"))

    def fail_commit():
        raise database_error

    session = SimpleNamespace(add=lambda _provider: None, commit=fail_commit)
    original = AntigravityProviderResultUnknown(QUOTA_CODE)
    with pytest.raises(AntigravityProviderResultUnknown) as raised:
        _failure(session, _provider(), lane=lane, error=original, commit=True)

    assert raised.value is original
    assert raised.value.__cause__ is database_error
