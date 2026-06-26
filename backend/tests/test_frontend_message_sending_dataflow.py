from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.no_postgres


def _function_body(source: str, function_name: str) -> str:
    start = source.index(f"\n  async function {function_name}")
    candidates = [
        source.find("\n  async function", start + 1),
        source.find("\n  function", start + 1),
        source.find("\n\n  return", start + 1),
    ]
    end = min(candidate for candidate in candidates if candidate != -1)
    return source[start:end]


def test_message_sending_account_target_loads_preserve_partial_success():
    source = (PROJECT_ROOT / "frontend/src/app/views/MessageSendingView.tsx").read_text()
    effect_start = source.index("React.useEffect(() => {\n    if (!accountId)")
    effect_end = source.index("\n  React.useEffect(() => {\n    if (!accountId) return undefined;", effect_start)
    effect_body = source[effect_start:effect_end]

    assert "Promise.allSettled" in effect_body
    assert "contactResult.status === 'fulfilled'" in effect_body
    assert "targetResult.status === 'fulfilled'" in effect_body
    assert "setContacts(contactResult.value)" in effect_body
    assert "setOperationTargets(targetResult.value)" in effect_body
    assert "setContacts([])" in effect_body
    assert "setOperationTargets([])" in effect_body
    assert "读取账号联系人失败" in effect_body
    assert "读取运营目标失败" in effect_body
    assert "allowedTargetKeys.has(key)" in effect_body
    assert "key.startsWith('manual:') || key.startsWith('private:')" in effect_body
    assert "读取账号联系人和运营目标失败" not in effect_body


def test_message_sending_target_loads_bind_account_and_request_sequence():
    source = (PROJECT_ROOT / "frontend/src/app/views/MessageSendingView.tsx").read_text()
    account_effect_start = source.index("React.useEffect(() => {\n    if (!accountId)")
    account_effect_end = source.index("\n  React.useEffect(() => {\n    if (!accountId) return undefined;", account_effect_start)
    account_effect = source[account_effect_start:account_effect_end]
    periodic_effect_start = account_effect_end
    periodic_effect = source[periodic_effect_start:source.index("\n\n  React.useEffect(() => {\n    function refreshMessageSendingData", periodic_effect_start)]

    assert "const messageContactsRequestRef = React.useRef({ accountId: undefined as number | undefined, seq: 0 });" in source
    assert "const messageTargetsRequestRef = React.useRef({ accountId: undefined as number | undefined, seq: 0 });" in source
    assert "function beginMessageContactsRequest(targetAccountId: number | undefined)" in source
    assert "function isActiveMessageContactsRequest(targetAccountId: number | undefined, requestSeq: number)" in source
    assert "function beginMessageTargetsRequest(targetAccountId: number | undefined)" in source
    assert "function isActiveMessageTargetsRequest(targetAccountId: number | undefined, requestSeq: number)" in source

    assert "const contactRequestSeq = beginMessageContactsRequest(accountId);" in account_effect
    assert "const targetRequestSeq = beginMessageTargetsRequest(accountId);" in account_effect
    assert "if (isActiveMessageContactsRequest(accountId, contactRequestSeq))" in account_effect
    assert "if (isActiveMessageTargetsRequest(accountId, targetRequestSeq))" in account_effect
    assert "if (isActiveMessageContactsRequest(accountId, contactRequestSeq) || isActiveMessageTargetsRequest(accountId, targetRequestSeq))" in account_effect

    assert "const requestSeq = beginMessageTargetsRequest(accountId);" in periodic_effect
    assert "if (!isActiveMessageTargetsRequest(accountId, requestSeq)) return;" in periodic_effect
    assert periodic_effect.index("if (!isActiveMessageTargetsRequest(accountId, requestSeq)) return;") < periodic_effect.index("setOperationTargets(items);")
    assert "if (isActiveMessageTargetsRequest(accountId, requestSeq)) setError(`刷新运营目标失败：${errorText(err)}`);" in periodic_effect


def test_message_sending_periodic_refresh_surfaces_backend_error():
    source = (PROJECT_ROOT / "frontend/src/app/views/MessageSendingView.tsx").read_text()
    effect_start = source.index("React.useEffect(() => {\n    function refreshMessageSendingData")
    effect_body = source[effect_start:source.index("\n\n  const onlineAccounts", effect_start)]

    assert "void onRefresh().catch((error: unknown) => {" in effect_body
    assert "setError(`刷新消息发送数据失败：${errorText(error)}`);" in effect_body
    assert "window.setInterval(refreshMessageSendingData, 60000)" in effect_body
    assert "void onRefresh(), 60000" not in effect_body


def test_message_sending_inline_material_create_distinguishes_refresh_failure():
    source = (PROJECT_ROOT / "frontend/src/app/views/MessageSendingView.tsx").read_text()
    create_block = source[source.index("async function createMaterial"):source.index("\n\n  function applyManualTarget")]

    assert "let created: Material;" in create_block
    assert "created = await api<Material>('/materials'" in create_block
    assert "const nextError = `创建素材失败：${errorText(materialError)}`;" in create_block
    assert "await onRefresh();" in create_block
    assert "const refreshError = `刷新消息发送数据失败：${errorText(error)}`;" in create_block
    assert "setError(refreshError);" in create_block

    refresh_block = create_block[create_block.index("await onRefresh()"):]
    assert "创建素材失败" not in refresh_block


def test_message_sending_preflight_ignores_stale_payload_results():
    source = (PROJECT_ROOT / "frontend/src/app/views/MessageSendingView.tsx").read_text()
    open_confirm = _function_body(source, "openConfirm")

    assert "const preflightRequestRef = React.useRef({ seq: 0, payloadSignature: '' });" in source
    assert "function messageSendPayloadSignature(payload: MessageSendBatchCreate)" in source
    assert "function beginPreflightRequest(payloadSignature: string)" in source
    assert "function isLatestPreflightRequest(requestSeq: number)" in source
    assert "function isCurrentPreflightRequest(requestSeq: number, payloadSignature: string)" in source

    assert "payloadSignature = messageSendPayloadSignature(payload);" in open_confirm
    assert "requestSeq = beginPreflightRequest(payloadSignature);" in open_confirm
    assert "if (!isCurrentPreflightRequest(requestSeq, payloadSignature)) return;" in open_confirm
    assert open_confirm.index("if (!isCurrentPreflightRequest(requestSeq, payloadSignature)) return;") > open_confirm.index("const result = await api<RiskPreflight>")
    assert open_confirm.index("if (!isCurrentPreflightRequest(requestSeq, payloadSignature)) return;") < open_confirm.index("setPreflight(result);")
    assert "if (requestSeq && !isCurrentPreflightRequest(requestSeq, payloadSignature)) return;" in open_confirm[open_confirm.index("} catch (validationError) {"):]
    assert "if (!requestSeq || isLatestPreflightRequest(requestSeq)) setPreflightLoading(false);" in open_confirm


def test_message_sending_submit_requires_current_confirmed_preflight_payload():
    source = (PROJECT_ROOT / "frontend/src/app/views/MessageSendingView.tsx").read_text()
    open_confirm = _function_body(source, "openConfirm")
    submit = _function_body(source, "submit")

    assert "const confirmedPreflightPayloadRef = React.useRef<{ payload: MessageSendBatchCreate | null; signature: string }>({ payload: null, signature: '' });" in source
    assert "function clearConfirmedPreflightPayload()" in source
    assert "function setConfirmedPreflightPayload(payload: MessageSendBatchCreate, signature: string)" in source
    assert "function currentConfirmedPreflightPayload()" in source

    assert "setConfirmedPreflightPayload(payload, payloadSignature);" in open_confirm
    assert open_confirm.index("setConfirmedPreflightPayload(payload, payloadSignature);") < open_confirm.index("setConfirmOpen(true);")

    assert "const currentPayload = buildPayload();" in submit
    assert "const confirmed = currentConfirmedPreflightPayload();" in submit
    assert "messageSendPayloadSignature(currentPayload) !== confirmed.signature" in submit
    assert "const nextError = '发送内容已变化，请重新进行风控预检';" in submit
    assert "return;" in submit[submit.index("const nextError = '发送内容已变化，请重新进行风控预检';"):]
    assert "const created = await createMessageSendTask(confirmed.payload);" in submit
    assert "clearConfirmedPreflightPayload();" in submit


def test_message_send_create_write_failure_uses_global_error_outlet():
    source = (PROJECT_ROOT / "frontend/src/app/context/messageActions.ts").read_text()
    body = _function_body(source, "createMessageSendTask")

    assert "catch (error)" in body
    assert "params.handleActionError(error);" in body
    assert "throw error;" in body

    catch_block = body[body.index("catch (error)"):]
    assert catch_block.index("params.handleActionError(error);") < catch_block.index("throw error;")

    refresh_block = body[body.index("await refreshMessageDataAfterAction("):]
    assert "params.handleActionError(error);" not in refresh_block[:refresh_block.index("catch (error)")]
