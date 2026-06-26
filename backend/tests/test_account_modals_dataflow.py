from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.no_postgres


def test_account_detail_actions_distinguish_detail_refresh_failure():
    source = (PROJECT_ROOT / "frontend/src/app/views/AccountModals.tsx").read_text()
    sync_block = source[source.index("async function syncTargets"):source.index("\n  async function manualSendNow")]
    manual_block = source[source.index("async function manualSendNow"):source.index("\n\n  const groupColumns")]

    assert "const [detailRefreshError, setDetailRefreshError] = React.useState('');" in source
    assert 'message="刷新账号详情失败"' in source
    assert "async function refreshAccountDetailAfterAction(accountId: number)" in source
    assert "setDetailRefreshError(error instanceof Error ? error.message : String(error));" in source

    assert "const accountId = accountDetail.account.id;" in sync_block
    assert "await api(`/tg-accounts/${accountId}/sync-targets`, { method: 'POST' });" in sync_block
    assert "if (!isActiveAccountDetail(accountId)) return;" in sync_block
    assert "await refreshAccountDetailAfterAction(accountId);" in sync_block
    sync_refresh_block = sync_block[sync_block.index("await refreshAccountDetailAfterAction"):]
    assert "setActionError(" not in sync_refresh_block[:sync_refresh_block.index("} catch")]

    assert "const accountId = accountDetail.account.id;" in manual_block
    assert "const targetId = manualTargetId;" in manual_block
    assert "const content = manualContent.trim();" in manual_block
    assert "if (!isActiveAccountDetail(accountId)) return;" in manual_block
    assert "await refreshAccountDetailAfterAction(accountId);" in manual_block
    manual_refresh_block = manual_block[manual_block.index("await refreshAccountDetailAfterAction"):]
    assert "setActionError(" not in manual_refresh_block[:manual_refresh_block.index("} catch")]


def test_verification_challenge_dialog_guards_current_account_task_and_session():
    source = (PROJECT_ROOT / "frontend/src/app/views/AccountModals.tsx").read_text()
    load_block = source[source.index("async function loadVerificationContext"):source.index("\n  async function refreshVerificationContext")]
    refresh_block = source[source.index("async function refreshVerificationContext"):source.index("\n  function openVerificationChallenge")]
    open_block = source[source.index("function openVerificationChallenge"):source.index("\n  async function submitVerificationReply")]
    submit_block = source[source.index("async function submitVerificationReply"):source.index("\n  async function loadSecurityDetail")]

    assert "const activeVerificationChallengeTaskId = React.useRef<number | null>(null);" in source
    assert "const activeVerificationChallengeSession = React.useRef(0);" in source
    assert "function beginVerificationChallengeSession(taskId: number)" in source
    assert "function isActiveVerificationChallenge(accountId: number, taskId: number, sessionId: number)" in source
    assert "function closeVerificationChallenge()" in source

    assert "const accountId = accountDetail.account.id;" in load_block
    assert "const taskId = task.id;" in load_block
    assert "if (!isActiveVerificationChallenge(accountId, taskId, sessionId)) return;" in load_block
    assert "setActionError(error instanceof Error ? `读取验证聊天失败：${error.message}` : '读取验证聊天失败');" in load_block

    assert "const accountId = accountDetail.account.id;" in refresh_block
    assert "const taskId = task.id;" in refresh_block
    assert "const sessionId = beginVerificationChallengeSession(taskId);" in refresh_block
    assert "if (!isActiveVerificationChallenge(accountId, taskId, sessionId)) return;" in refresh_block
    assert "setActionError(error instanceof Error ? `重新读取验证聊天失败：${error.message}` : '重新读取验证聊天失败');" in refresh_block

    assert "const sessionId = beginVerificationChallengeSession(task.id);" in open_block
    assert "void loadVerificationContext(task, sessionId);" in open_block

    assert "const accountId = accountDetail.account.id;" in submit_block
    assert "const taskId = task.id;" in submit_block
    assert "const sessionId = activeVerificationChallengeSession.current;" in submit_block
    assert "if (!isActiveVerificationChallenge(accountId, taskId, sessionId)) return;" in submit_block
    assert "setVerificationReplies((current) => ({ ...current, [taskId]: '' }));" in submit_block
    assert "closeVerificationChallenge();" in submit_block
    assert "setActionError(error instanceof Error ? `提交验证回复失败：${error.message}` : '提交验证回复失败');" in submit_block

    assert "onCancel={closeVerificationChallenge}" in source
    assert "onClick={() => openVerificationChallenge(task)}" in source
    assert "onClick={() => { void refreshVerificationContext(verificationChallengeTask); }}" in source
    assert "onSearch={() => { void submitVerificationReply(verificationChallengeTask); }}" in source
