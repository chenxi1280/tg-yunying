from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _function_body(source: str, function_name: str) -> str:
    start = source.index(f"function {function_name}")
    next_function = source.find("\nfunction ", start + 1)
    end = len(source) if next_function == -1 else next_function
    return source[start:end]


def test_group_management_detail_panels_close_when_group_detail_load_fails():
    view = (PROJECT_ROOT / "frontend/src/app/views/GroupManagementView.tsx").read_text()
    context_types = (PROJECT_ROOT / "frontend/src/app/context/types.ts").read_text()
    account_actions = (PROJECT_ROOT / "frontend/src/app/context/accountActions.ts").read_text()

    assert "onOpenGroupDetail: (group: Group) => Promise<boolean>;" in view
    assert "openGroupDetail: (group: Group) => Promise<boolean>;" in context_types
    assert "async function openGroupDetail(group: Group): Promise<boolean>" in account_actions

    open_group_detail = account_actions[account_actions.index("async function openGroupDetail"):account_actions.index("\n\n  function avatarUrl")]
    assert "return true;" in open_group_detail
    assert open_group_detail.count("return false;") >= 2
    assert "params.handleActionError(error);" in open_group_detail

    coverage_panel = _function_body(view, "GroupCoveragePanel")
    assert "async function openCoverageDetail()" in coverage_panel
    assert "const loaded = await onOpenGroupDetail(group);" in coverage_panel
    assert "if (!loaded) setDetailOpen(false);" in coverage_panel
    assert "void openCoverageDetail()" in coverage_panel

    listener_panel = _function_body(view, "ListenerContextPanel")
    assert "async function openListenerDetail()" in listener_panel
    assert "const loaded = await onOpenGroupDetail(group);" in listener_panel
    assert "if (!loaded) setDetailOpen(false);" in listener_panel
    assert "void openListenerDetail()" in listener_panel

    assert "setDetailOpen(true); void onOpenGroupDetail(selectedGroup);" not in view
