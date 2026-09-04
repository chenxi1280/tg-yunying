import pytest
from pydantic import ValidationError

from app import schemas
from app.services.task_center import service
from tests.test_engagement_account_binding import _session, _seed


pytestmark = pytest.mark.no_postgres


@pytest.mark.parametrize("kind", ["view", "like", "comment"])
def test_three_channel_task_entries_persist_source_controls_and_group_binding(kind):
    schema = getattr(schemas, f"Channel{kind.title()}TaskCreate")
    create = getattr(service, f"create_channel_{kind}_task")
    with _session() as session:
        _seed(session)
        payload = schema(name=f"source {kind}", target_channel_id=101,
            engagement_contract_version="unified_engagement_v1", account_group_ids=[2, 1],
            initial_historical_post_limit=0, source_expectation_mode="promised_daily_sources")
        task = create(session, 1, payload, "tester")
        session.commit()
        session.refresh(task)
        assert task.type_config["initial_historical_post_limit"] == 0
        assert task.type_config["source_expectation_mode"] == "promised_daily_sources"
        assert task.account_config["account_group_ids"] == [1, 2]
        assert task.account_config["selection_mode"] == "group"


@pytest.mark.parametrize("kind", ["view", "like", "comment"])
@pytest.mark.parametrize("invalid", [dict(initial_historical_post_limit=11), dict(source_expectation_mode="unknown")])
def test_source_controls_reject_out_of_contract_values(kind, invalid):
    schema = getattr(schemas, f"Channel{kind.title()}TaskCreate")
    with pytest.raises(ValidationError):
        schema(name="invalid", target_channel_id=101, **invalid)
