from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_account_detail_open_result_controls_deep_link_tab_switch() -> None:
    app_shell = (PROJECT_ROOT / "frontend/src/app/AppShell.tsx").read_text()
    account_actions = (PROJECT_ROOT / "frontend/src/app/context/accountActions.ts").read_text()
    context_types = (PROJECT_ROOT / "frontend/src/app/context/types.ts").read_text()
    account_modals = (PROJECT_ROOT / "frontend/src/app/views/AccountModals.tsx").read_text()

    deep_link_effect = app_shell[
        app_shell.index("if (activeView !== 'accounts') return;"):
        app_shell.index("\n  const loginReady", app_shell.index("if (activeView !== 'accounts') return;"))
    ]
    open_detail = account_actions[
        account_actions.index("async function openAccountDetail"):
        account_actions.index("\n  async function openAccountVerificationCodes")
    ]
    operation_deep_link = app_shell[
        app_shell.index("async function openAccountDetailFromOperation"):
        app_shell.index("\n  async function openTaskFromGroup")
    ]

    assert "openAccountDetail: (account: Account) => Promise<boolean>;" in context_types
    assert "onOpenAccountDetail: (account: Account) => Promise<boolean>;" in account_modals
    assert "async function openAccountDetail(account: Account): Promise<boolean>" in account_actions
    assert "return true;" in open_detail
    assert "return false;" in open_detail
    assert "params.handleActionError(error);" in open_detail
    assert ".then((opened) => {" in deep_link_effect
    assert "if (opened) setAccountDetailTab(tab);" in deep_link_effect
    assert "const opened = await openAccountDetail" in operation_deep_link
    assert "if (opened) setAccountDetailTab(tab);" in operation_deep_link
