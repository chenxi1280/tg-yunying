from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TASK_CENTER_VIEW = PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx"
TASK_CENTER_VIEW_MODEL = PROJECT_ROOT / "frontend/src/app/views/taskCenterViewModel.ts"
TASK_CENTER_WIZARD = PROJECT_ROOT / "frontend/src/app/views/TaskCenterWizardSections.tsx"
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


def test_rank_deboost_wizard_exposes_only_business_inputs() -> None:
    wizard = _source(TASK_CENTER_WIZARD)
    simple_config = wizard[wizard.index("function SimpleSearchClickConfig"):wizard.index("\n\nexport function WizardTypeConfig")]
    type_config = wizard[wizard.index("export function WizardTypeConfig"):wizard.index("\n\nexport function WizardOperationProfile")]

    assert "name=\"keywords\"" in simple_config
    assert "name=\"target_count\"" in simple_config
    assert "账号、代理和执行节奏" in simple_config
    assert "if (simpleSearchCreation && (taskType === 'search_join_group' || taskType === 'search_rank_deboost'))" in type_config
    assert "排名观察账号选择" not in wizard
    assert "proxy_airport_node_id" not in wizard


def test_rank_deboost_payload_uses_only_three_business_fields() -> None:
    view = _source(TASK_CENTER_VIEW)
    payload = view[view.index("function simpleSearchClickPayload"):view.index("\n\n  function parseExcludedSenderInput")]
    create_payload = view[view.index("function createPayload"):view.index("\n\n  function settingsPayload")]

    assert "target_operation_target_id: values.target_operation_target_id" in payload
    assert "const keywords = words(values.keywords);" in payload
    assert "keywords," in payload
    assert "target_count: values.target_count" in payload
    assert "account_config" not in payload
    assert "account_pool_id:" not in payload
    assert "proxy_airport_node_id:" not in payload
    assert "if (isSimpleSearchClickTask(taskType)) return simpleSearchClickPayload(values);" in create_payload


def test_rank_deboost_step_and_submit_fields_exclude_system_controls() -> None:
    view_model = _source(TASK_CENTER_VIEW_MODEL)
    step_block = view_model[view_model.index("export function fieldsForStep"):view_model.index("\n\nexport function accountSelectionFields")]
    submit_block = view_model[view_model.index("export function fieldsForSubmit"):view_model.index("\n\nexport function editFieldsForSubmit")]
    edit_block = view_model[view_model.index("export function editFieldsForSubmit"):]

    assert "if (step === 2 && isSimpleSearchClickTask(taskType)) return ['keywords', 'target_count'];" in step_block
    assert "if (step === 3 && isSimpleSearchClickTask(taskType)) return [];" in step_block
    assert "if (isSimpleSearchClickTask(taskType)) return ['target_operation_target_id', 'keywords', 'target_count'];" in submit_block
    assert "if (isSimpleSearchClickTask(taskType)) return ['target_operation_target_id', 'keywords', 'target_count'];" in edit_block
