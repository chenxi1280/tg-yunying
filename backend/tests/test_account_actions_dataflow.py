from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.no_postgres


def test_account_profile_save_binds_account_payload_avatar_and_request_sequence():
    context = (PROJECT_ROOT / "frontend/src/app/context.tsx").read_text()
    source = (PROJECT_ROOT / "frontend/src/app/context/accountActions.ts").read_text()
    save_body = source[source.index("async function saveAccountProfile"):source.index("\n\n  async function retryAccountProfileSync")]

    assert "const accountProfileSaveRequestRef = React.useRef({ seq: 0, accountId: null as number | null, signature: '' });" in context
    assert "accountProfileSaveRequestRef," in context
    assert "accountProfileSaveRequestRef: { current: { seq: number; accountId: number | null; signature: string } };" in source

    assert "function accountProfilePayload()" in source
    assert "function accountAvatarFileSignature(file: File | null)" in source
    assert "function accountProfileSaveSignature(accountId: number, payload: ReturnType<typeof accountProfilePayload>, avatarFile: File | null)" in source
    assert "function beginAccountProfileSaveRequest(accountId: number, signature: string)" in source
    assert "function isCurrentAccountProfileSaveRequest(requestSeq: number)" in source
    assert "function isActiveAccountProfileSaveRequest(accountId: number, requestSeq: number, signature: string)" in source

    assert "const accountId = params.accountDetail.account.id;" in save_body
    assert "const payload = accountProfilePayload();" in save_body
    assert "const avatarFile = params.avatarFile;" in save_body
    assert "const signature = accountProfileSaveSignature(accountId, payload, avatarFile);" in save_body
    assert "const requestSeq = beginAccountProfileSaveRequest(accountId, signature);" in save_body
    assert "if (!isActiveAccountProfileSaveRequest(accountId, requestSeq, signature)) return;" in save_body
    assert save_body.index("if (!isActiveAccountProfileSaveRequest(accountId, requestSeq, signature)) return;") < save_body.index("await api<Account>(`/tg-accounts/${accountId}/profile`")
    assert save_body.index("if (!isActiveAccountProfileSaveRequest(accountId, requestSeq, signature)) return;", save_body.index("await api<Account>(`/tg-accounts/${accountId}/profile`")) < save_body.index("params.closeModal();")
    assert save_body.index("if (!isActiveAccountProfileSaveRequest(accountId, requestSeq, signature)) return;", save_body.index("catch")) < save_body.index("params.handleActionError(error);")
    assert "if (isCurrentAccountProfileSaveRequest(requestSeq)) params.setBusy('');" in save_body
    assert "`/tg-accounts/${params.accountDetail.account.id}/profile`" not in save_body
    assert "JSON.stringify({ ...params.profileForm" not in save_body


def test_account_detail_actions_bind_account_action_and_request_sequence():
    context = (PROJECT_ROOT / "frontend/src/app/context.tsx").read_text()
    source = (PROJECT_ROOT / "frontend/src/app/context/accountActions.ts").read_text()
    contacts_body = source[source.index("async function syncAccountContacts"):source.index("\n\n  async function queueAccountSyncNow")]
    sync_body = source[source.index("async function queueAccountSyncNow"):source.index("\n\n  async function openGroupDetail")]
    retry_body = source[source.index("async function retryAccountProfileSync"):source.index("\n\n  async function runLogin")]

    assert "const accountDetailActionRequestRef = React.useRef({ seq: 0, accountId: null as number | null, action: '' });" in context
    assert "accountDetailActionRequestRef," in context
    assert "accountDetailActionRequestRef: { current: { seq: number; accountId: number | null; action: string } };" in source

    assert "function beginAccountDetailActionRequest(accountId: number, action: string)" in source
    assert "function isCurrentAccountDetailActionRequest(requestSeq: number)" in source
    assert "function isActiveAccountDetailActionRequest(accountId: number, action: string, requestSeq: number)" in source

    for body, action, success_text in [
        (contacts_body, "sync-contacts", "联系人已同步"),
        (sync_body, "sync-now", "同步完成"),
        (retry_body, "profile-sync-retry", "已重新提交"),
    ]:
        assert "const accountId = params.accountDetail.account.id;" in body
        assert f"const action = '{action}';" in body
        assert "const requestSeq = beginAccountDetailActionRequest(accountId, action);" in body
        assert "if (!isActiveAccountDetailActionRequest(accountId, action, requestSeq)) return;" in body
        assert body.index("if (!isActiveAccountDetailActionRequest(accountId, action, requestSeq)) return;") < body.index("await refreshAccountCenterDataAfterAction(")
        assert body.index("if (!isActiveAccountDetailActionRequest(accountId, action, requestSeq)) return;") < body.index(f"params.showResult('{success_text}'")
        assert body.index("if (!isActiveAccountDetailActionRequest(accountId, action, requestSeq)) return;", body.index("catch")) < body.index("params.handleActionError(error);")
        assert "if (isCurrentAccountDetailActionRequest(requestSeq)) params.setBusy('');" in body

    assert "`/tg-accounts/${params.accountDetail.account.id}/contacts/sync`" not in contacts_body
    assert "`/tg-accounts/${params.accountDetail.account.id}/sync-now`" not in sync_body
    assert "`/tg-accounts/${params.accountDetail.account.id}/profile-sync/retry`" not in retry_body


def test_account_detail_mutations_bind_account_action_and_request_sequence():
    source = (PROJECT_ROOT / "frontend/src/app/context/accountActions.ts").read_text()
    move_body = source[source.index("async function moveCurrentAccountPool"):source.index("\n\n  async function createClonePlan")]
    create_body = source[source.index("async function createClonePlan"):source.index("\n\n  async function confirmClonePlan")]
    confirm_body = source[source.index("async function confirmClonePlan"):source.index("\n\n  async function retryCloneItem")]
    retry_body = source[source.index("async function retryCloneItem"):source.index("\n\n  async function confirmVerificationTask")]

    for body, action in [
        (move_body, "move-pool"),
        (create_body, "clone-create"),
    ]:
        assert "const accountId = params.accountDetail.account.id;" in body
        assert f"const action = '{action}';" in body
        assert "const requestSeq = beginAccountDetailActionRequest(accountId, action);" in body
        assert "if (!isActiveAccountDetailActionRequest(accountId, action, requestSeq)) return;" in body
        assert body.index("if (!isActiveAccountDetailActionRequest(accountId, action, requestSeq)) return;") < body.index("params.showResult(")
        assert body.index("if (!isActiveAccountDetailActionRequest(accountId, action, requestSeq)) return;") < body.index("await refreshAccountCenterDataAfterAction(")
        assert body.index("if (!isActiveAccountDetailActionRequest(accountId, action, requestSeq)) return;", body.index("catch")) < body.index("params.handleActionError(error);")
        assert "if (isCurrentAccountDetailActionRequest(requestSeq)) params.setBusy('');" in body

    for body, action_prefix in [
        (confirm_body, "clone-confirm"),
        (retry_body, "clone-retry"),
    ]:
        assert "if (!params.accountDetail) return;" in body
        assert "const accountId = params.accountDetail.account.id;" in body
        assert f"const action = `{action_prefix}:${{" in body
        assert "const requestSeq = beginAccountDetailActionRequest(accountId, action);" in body
        assert "if (!isActiveAccountDetailActionRequest(accountId, action, requestSeq)) return;" in body
        assert body.index("if (!isActiveAccountDetailActionRequest(accountId, action, requestSeq)) return;") < body.index("params.showResult(")
        assert body.index("if (!isActiveAccountDetailActionRequest(accountId, action, requestSeq)) return;") < body.index("await refreshAccountCenterDataAfterAction(")
        assert body.index("if (!isActiveAccountDetailActionRequest(accountId, action, requestSeq)) return;", body.index("catch")) < body.index("params.handleActionError(error);")
        assert "if (isCurrentAccountDetailActionRequest(requestSeq)) params.setBusy('');" in body

    assert "`/tg-accounts/${params.accountDetail.account.id}/move-pool`" not in move_body
    assert "source_account_id: params.accountDetail.account.id" not in create_body


def test_account_login_actions_bind_account_action_and_request_sequence():
    context = (PROJECT_ROOT / "frontend/src/app/context.tsx").read_text()
    source = (PROJECT_ROOT / "frontend/src/app/context/accountActions.ts").read_text()
    start_body = source[source.index("async function startOrResumeAccountLogin"):source.index("\n\n  async function completeAccountLogin")]
    complete_body = source[source.index("async function completeAccountLogin"):source.index("\n\n  async function createAccount")]
    code_body = source[source.index("async function submitAccountLoginCode"):source.index("\n\n  async function submitAccountLoginPassword")]
    password_body = source[source.index("async function submitAccountLoginPassword"):source.index("\n\n  async function resendAccountLoginCode")]
    qr_body = source[source.index("async function checkAccountQrLogin"):source.index("\n\n  async function healthCheck")]

    assert "const accountLoginRequestRef = React.useRef({ seq: 0, accountId: null as number | null, action: '' });" in context
    assert "accountLoginRequestRef," in context
    assert "accountLoginRequestRef: { current: { seq: number; accountId: number | null; action: string } };" in source

    assert "interface AccountLoginRequest" in source
    assert "function beginAccountLoginRequest(accountId: number, action: string)" in source
    assert "function isCurrentAccountLoginRequest(requestSeq: number)" in source
    assert "function isActiveAccountLoginRequest(request: AccountLoginRequest)" in source

    for body, action in [
        (start_body, "${resend ? 'resend' : method}"),
        (code_body, "code-submit"),
        (password_body, "password-submit"),
        (qr_body, "qr-check"),
    ]:
        assert f"const action = `{action}`;" in body or f"const action = '{action}';" in body
        assert "const requestSeq = beginAccountLoginRequest(accountId, action);" in body
        assert "const request = { accountId, action, requestSeq };" in body
        assert "if (!isActiveAccountLoginRequest(request)) return;" in body

    assert "if (!isActiveAccountLoginRequest(request)) return;" in complete_body
    assert "completeAccountLogin(updated, request)" in code_body
    assert "completeAccountLogin(updated, request)" in password_body
    assert "completeAccountLogin(updated, request)" in qr_body
    assert "setAccountLoginErrorIfActive(request, error);" in start_body
    assert "setAccountLoginErrorIfActive(request, error);" in code_body
    assert "setAccountLoginErrorIfActive(request, error);" in password_body
    assert "setAccountLoginErrorIfActive(request, error);" in qr_body
    assert "params.setAccountLoginForm((current) => ({ ...current, error: params.errorMessage(error) }));" not in start_body
    assert "params.setAccountLoginForm((current) => ({ ...current, error: params.errorMessage(error) }));" not in code_body
    assert "params.setAccountLoginForm((current) => ({ ...current, error: params.errorMessage(error) }));" not in password_body
    assert "params.setAccountLoginForm((current) => ({ ...current, error: params.errorMessage(error) }));" not in qr_body
