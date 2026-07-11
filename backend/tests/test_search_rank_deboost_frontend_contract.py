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


def test_frontend_types_and_helpers_model_rank_deboost_groups() -> None:
    accounts = _source(ACCOUNT_TYPES)
    view_model = _source(TASK_CENTER_VIEW_MODEL)

    assert "pool_purpose: 'normal' | 'code_receiver' | 'rank_deboost' | string;" in accounts
    assert "export function isOperationalAccount(account: Account, accountPools: AccountPool[])" in view_model
    assert "export function isEligibleRankAccount(account: Account, accountPools: AccountPool[])" in view_model
    assert "export function rankPoolSummaries(accountPools: AccountPool[])" in view_model
    assert "export function accountSelectionPreview(values: Record<string, any>, accounts: Account[], accountPools: AccountPool[], taskType: TaskCenterTaskType)" in view_model
    assert "return accountSelectionPreview(values, accounts, accountPools, 'search_rank_deboost');" in view_model


def test_rank_deboost_wizard_exposes_dedicated_account_selector() -> None:
    wizard = _source(TASK_CENTER_WIZARD)
    type_config_start = wizard.index("export function WizardTypeConfig")
    rank_start = wizard.index("if (taskType === 'search_rank_deboost')", type_config_start)
    rank_block = wizard[rank_start:wizard.index("\n  return (", rank_start)]
    account_start = wizard.index("if (taskType === 'search_rank_deboost')", wizard.index("export function WizardAccounts"))
    account_block = wizard[account_start:wizard.index("\n\nexport function WizardReview", account_start)]

    assert "const rankDeboostPools = rankPoolSummaries(accountPools);" in rank_block
    assert "proxy_airport_node_id" not in rank_block
    assert "account_pool_id" not in rank_block
    assert "missingBinding" in rank_block

    assert "排名观察账号选择" in account_block
    assert "selection_mode" in account_block
    assert "account_group_id" in account_block
    assert "account_ids" in account_block
    assert "isEligibleRankAccount(account, accountPools)" in account_block
    assert "搜索排名观察任务的账号分组和代理节点在「任务配置」步骤中设置" not in wizard


def test_rank_deboost_payload_uses_account_config_not_task_level_proxy_node() -> None:
    view = _source(TASK_CENTER_VIEW)
    payload = view[view.index("function searchRankDeboostPayload"):view.index("\n\n  function parseExcludedSenderInput")]
    create_payload = view[view.index("if (taskType === 'search_rank_deboost')"):view.index("\n    if (taskType === 'group_ai_chat')")]

    assert "account_config: accountConfig(values)" in payload
    assert "account_pool_id:" not in payload
    assert "proxy_airport_node_id:" not in payload
    assert "Number(values.proxy_airport_node_id)" not in payload
    assert "Number(values.account_pool_id)" not in payload
    assert "return searchRankDeboostPayload(values);" in create_payload


def test_rank_deboost_step_and_submit_fields_include_account_selection() -> None:
    view_model = _source(TASK_CENTER_VIEW_MODEL)
    step_block = view_model[view_model.index("export function fieldsForStep"):view_model.index("\n\nexport function accountSelectionFields")]
    submit_block = view_model[view_model.index("export function fieldsForSubmit"):view_model.index("\n\nexport function editFieldsForSubmit")]

    assert "if (step === 2 && taskType === 'search_rank_deboost') return ['keywords'];" in step_block
    assert "if (step === 3 && taskType === 'search_rank_deboost') return accountSelectionFields(accountMode);" in step_block
    assert "...accountSelectionFields(accountMode)" in submit_block
    assert "'account_pool_id'" not in submit_block[submit_block.index("if (taskType === 'search_rank_deboost')"):]
    assert "'proxy_airport_node_id'" not in submit_block[submit_block.index("if (taskType === 'search_rank_deboost')"):]
