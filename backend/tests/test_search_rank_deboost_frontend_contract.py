from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TASK_CENTER_VIEW = PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx"
TASK_CENTER_VIEW_MODEL = PROJECT_ROOT / "frontend/src/app/views/taskCenterViewModel.ts"
TASK_CENTER_WIZARD = PROJECT_ROOT / "frontend/src/app/views/TaskCenterWizardSections.tsx"
TASK_CENTER_TARGET = PROJECT_ROOT / "frontend/src/app/views/TaskCenterTargetSection.tsx"
ACCOUNT_TYPES = PROJECT_ROOT / "frontend/src/app/types/accounts.ts"


def _source(path: Path) -> str:
    return path.read_text()


def test_frontend_keeps_rank_deboost_as_a_system_managed_task_type() -> None:
    accounts = _source(ACCOUNT_TYPES)
    view_model = _source(TASK_CENTER_VIEW_MODEL)

    assert "pool_purpose: 'normal' | 'code_receiver' | 'rank_deboost' | string;" in accounts
    assert "export function isSimpleSearchClickTask(taskType: TaskCenterTaskType)" in view_model
    assert "return taskType === 'search_join_group' || taskType === 'search_rank_deboost';" in view_model
    assert "SIMPLE_SEARCH_CLICK_WIZARD_STEPS" in view_model


def test_search_click_wizard_exposes_operator_execution_controls() -> None:
    wizard = _source(TASK_CENTER_WIZARD)
    simple_config = wizard[wizard.index("function SimpleSearchClickConfig"):wizard.index("\n\nexport function WizardTypeConfig")]
    type_config = wizard[wizard.index("export function WizardTypeConfig"):wizard.index("\n\nexport function WizardOperationProfile")]

    assert "name=\"keywords\"" in simple_config
    assert "const targetField = isRankDeboost ? 'target_count' : 'daily_target_count';" in simple_config
    assert "每日目标次数" in simple_config
    assert "系统负责账号资格、代理、机器人和风险闸门" in simple_config
    assert "export function SearchClickExecutionConfig" in wizard
    assert "name=\"account_group_id\"" in wizard
    assert "name=\"max_actions_per_day\"" in wizard
    assert "name=\"scheduled_end\"" in wizard
    assert "name=\"daily_jitter_percent\"" in wizard
    assert "name=\"hourly_jitter_percent\"" in wizard
    assert "name=\"quiet_start\"" in wizard
    assert "name=\"quiet_end\"" in wizard
    assert "pool.pool_purpose === requiredPurpose && pool.is_enabled" in wizard
    assert "if (simpleSearchCreation && (taskType === 'search_join_group' || taskType === 'search_rank_deboost'))" in type_config
    assert "排名观察账号选择" not in wizard
    assert "proxy_airport_node_id" not in wizard


def test_search_click_payload_includes_operator_execution_controls() -> None:
    view = _source(TASK_CENTER_VIEW)
    payload = view[view.index("function simpleSearchClickPayload"):view.index("\n\n  function parseExcludedSenderInput")]
    create_payload = view[view.index("function createPayload"):view.index("\n\n  function settingsPayload")]

    assert "target_title: values.target_title?.trim()" in payload
    assert "target_link: values.target_link?.trim()" in payload
    assert "target_operation_target_id" not in payload
    assert "const keywords = words(values.keywords);" in payload
    assert "keywords," in payload
    assert "daily_target_count: values.daily_target_count" in payload
    assert "target_count: values.target_count" in payload
    assert "searchTaskType === 'search_join_group'" in payload
    assert "account_group_id: values.account_group_id" in payload
    assert "max_actions_per_day: values.max_actions_per_day" in payload
    assert "scheduled_end: fromBeijingDateTimeLocalValue(values.scheduled_end)" in payload
    assert "daily_jitter_percent: values.daily_jitter_percent" in payload
    assert "hourly_jitter_percent: values.hourly_jitter_percent" in payload
    assert "quiet_hours: quietHours" in payload
    assert "account_config" not in payload
    assert "account_pool_id:" not in payload
    assert "proxy_airport_node_id:" not in payload
    assert "if (isSimpleSearchClickTask(taskType)) return simpleSearchClickPayload(values);" in create_payload


def test_search_click_step_and_submit_fields_include_operator_controls() -> None:
    view_model = _source(TASK_CENTER_VIEW_MODEL)
    step_block = view_model[view_model.index("export function fieldsForStep"):view_model.index("\n\nexport function accountSelectionFields")]
    submit_block = view_model[view_model.index("export function fieldsForSubmit"):view_model.index("\n\nexport function editFieldsForSubmit")]
    edit_block = view_model[view_model.index("export function editFieldsForSubmit"):]

    assert "if (step === 2 && isSimpleSearchClickTask(taskType)) return ['keywords', simpleSearchTargetField(taskType)];" in step_block
    assert "if (step === 3 && isSimpleSearchClickTask(taskType)) return ['account_group_id', 'max_actions_per_day', 'scheduled_end', 'daily_jitter_percent', 'hourly_jitter_percent', 'quiet_start', 'quiet_end'];" in step_block
    assert "if (isSimpleSearchClickTask(taskType)) return ['target_title', 'target_link', 'keywords', simpleSearchTargetField(taskType), 'account_group_id', 'max_actions_per_day', 'scheduled_end', 'daily_jitter_percent', 'hourly_jitter_percent', 'quiet_start', 'quiet_end'];" in submit_block
    assert "if (isSimpleSearchClickTask(taskType)) return ['target_title', 'target_link', 'keywords', simpleSearchTargetField(taskType), 'account_group_id', 'max_actions_per_day', 'scheduled_end', 'daily_jitter_percent', 'hourly_jitter_percent', 'quiet_start', 'quiet_end'];" in edit_block


def test_search_click_target_step_uses_name_and_public_link_not_target_selector() -> None:
    target_section = _source(TASK_CENTER_TARGET)
    simple_target = target_section[
        target_section.index("function SimpleSearchClickTargetFields"):
        target_section.index("function GroupTaskTargetFields")
    ]

    assert 'name="target_title"' in simple_target
    assert 'name="target_link"' in simple_target
    assert "目标群完整名称" in simple_target
    assert "公开 Telegram 链接" in simple_target
    assert "GroupTargetField" not in simple_target
