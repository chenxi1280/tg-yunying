import pytest
from pydantic import ValidationError

from app import schemas
from app.models import OperationTarget
from app.services.task_center import service
from app.services.task_center.config_fields import COMMON_SETTINGS_FIELDS, TYPE_SETTINGS_FIELDS
from tests.test_engagement_account_binding import _session, _seed


pytestmark = pytest.mark.no_postgres
BINDING = dict(engagement_contract_version="unified_engagement_v1",
    account_selection_mode="group", account_group_ids=[1, 2], concurrency_limit_per_group=4)
SOURCE = dict(initial_historical_post_limit=0, source_expectation_mode="promised_daily_sources")
UPDATES = {
    "group_ai_chat": dict(daily_message_target=12, daily_target_jitter_bps=1000,
        attention_quiet_after_min_seconds=20, attention_quiet_after_max_seconds=40),
    "channel_like": dict(**SOURCE, daily_reaction_cap=80),
    "channel_comment": dict(**SOURCE, account_ratio_min_bps=5600, account_ratio_max_bps=6400),
    "channel_view": dict(**SOURCE, account_ratio_min_bps=8100, account_ratio_max_bps=9400,
        rolling_participation_days=4, view_exposure_mode="explicit_per_source",
        per_account_source_degree_min=1, per_account_source_degree_max=3,
        every_active_message=False, per_source_exposure_target=2, per_source_exposure_ratio_bps=None),
}


@pytest.mark.parametrize("task_type", UPDATES)
def test_engine_patch_fields_are_accepted_by_schema_and_allowlist(task_type):
    data = {**BINDING, **UPDATES[task_type]}
    payload = schemas.TaskSettingsUpdate.model_validate(data)
    assert payload.model_dump(exclude_unset=True) == data
    assert not set(data) - TYPE_SETTINGS_FIELDS[task_type] - COMMON_SETTINGS_FIELDS


@pytest.mark.parametrize("task_type", UPDATES)
def test_engine_settings_edit_persists_and_partial_edit_keeps_fields(task_type):
    with _session() as session:
        _seed(session)
        session.add(OperationTarget(id=102, tenant_id=1, target_type="group",
            tg_peer_id="-100102", title="test group"))
        session.commit()
        names = {"group_ai_chat": "GroupAIChat", "channel_like": "ChannelLike",
            "channel_comment": "ChannelComment", "channel_view": "ChannelView"}
        target = {"target_operation_target_id": 102, "topic_participation_rate": 0} if task_type == "group_ai_chat" else {"target_channel_id": 101}
        create = getattr(service, f"create_{task_type}_task")
        task = create(session, 1, getattr(schemas, names[task_type]+"TaskCreate")(
            name="before", **target, **BINDING), "test")
        updated = service.update_task_settings(session, 1, task.id,
            schemas.TaskSettingsUpdate(name="edited", **BINDING, **UPDATES[task_type]), "test")
        assert updated.name == "edited"
        for key, value in {**BINDING, **UPDATES[task_type]}.items():
            assert updated.type_config.get(key) == value
        expected = dict(updated.type_config)
        partial = service.update_task_settings(session, 1, task.id,
            schemas.TaskSettingsUpdate(name="name only"), "test")
        assert partial.type_config == expected
        assert partial.type_config["account_group_ids"] == [1, 2]


@pytest.mark.parametrize("fields", [dict(initial_historical_post_limit=11),
    dict(source_expectation_mode="bad"), dict(concurrency_limit_per_group=0), dict(unknown_setting=1)])
def test_invalid_patch_fields_still_rejected(fields):
    with pytest.raises(ValidationError):
        schemas.TaskSettingsUpdate(**fields)


@pytest.mark.parametrize("fields", [dict(account_ratio_min_bps=9600),
    dict(account_group_ids=[]), dict(account_group_ids=[1, 1]), dict(daily_reaction_cap=40)])
def test_partial_patch_keeps_merged_config_and_task_type_validation(fields):
    with _session() as session:
        _seed(session)
        task = service.create_channel_view_task(session, 1,
            schemas.ChannelViewTaskCreate(name="test", target_channel_id=101, **BINDING), "test")
        with pytest.raises(ValueError):
            service.update_task_settings(session, 1, task.id, schemas.TaskSettingsUpdate(**fields), "test")


def test_exposure_mode_switch_can_explicitly_clear_previous_target():
    with _session() as session:
        _seed(session)
        task = service.create_channel_view_task(session, 1,
            schemas.ChannelViewTaskCreate(name="test", target_channel_id=101,
                view_exposure_mode="explicit_per_source", per_source_exposure_target=2, **BINDING), "test")
        service.update_task_settings(session, 1, task.id, schemas.TaskSettingsUpdate(
            view_exposure_mode="natural_auto", per_source_exposure_target=None), "test")
        assert task.type_config["view_exposure_mode"] == "natural_auto"
        assert task.type_config.get("per_source_exposure_target") is None
