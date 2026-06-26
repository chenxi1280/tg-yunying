from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.no_postgres


def test_authorization_actions_distinguish_account_refresh_failure():
    source = (PROJECT_ROOT / "frontend/src/app/views/AccountAuthorizationAssetsPanel.tsx").read_text()
    complete_block = source[source.index("async function completeLoginModal"):source.index("\n  function confirmSwitch")]
    switch_block = source[source.index("async function switchPrimary"):source.index("\n  function closeLoginModal")]

    assert "const [refreshError, setRefreshError] = React.useState('');" in source
    assert 'message="刷新账号授权资产失败"' in source
    assert "async function refreshChangedAccount" in source
    assert "setRefreshError(error instanceof Error ? error.message : String(error));" in source

    assert "await refreshChangedAccount(targetAccountId, loginSeq);" in complete_block
    assert "await onChanged();" not in complete_block
    complete_refresh_block = complete_block[complete_block.index("await refreshChangedAccount"):]
    assert "setError(" not in complete_refresh_block

    assert "await refreshChangedAccount(targetAccountId);" in switch_block
    assert "await onChanged();" not in switch_block
    switch_refresh_block = switch_block[switch_block.index("await refreshChangedAccount"):]
    assert "setError(" not in switch_refresh_block[:switch_refresh_block.index("} catch")]


def test_standby_login_start_ignores_stale_payload_response():
    source = (PROJECT_ROOT / "frontend/src/app/views/AccountAuthorizationAssetsPanel.tsx").read_text()
    start_block = source[source.index("async function startStandbyLogin"):source.index("\n  async function verifyStandbyLogin")]

    assert "const latestLoginStartPayloadSignature = React.useRef('');" in source
    assert "const loginStartPayload = React.useMemo(() => ({" in source
    assert "const loginStartPayloadSignature = React.useMemo(() => JSON.stringify(loginStartPayload), [loginStartPayload]);" in source
    assert "latestLoginStartPayloadSignature.current = loginStartPayloadSignature;" in source
    assert "function isActiveLoginStart(targetAccountId: number, loginSeq: number, payloadSignature: string)" in source

    assert "const payload = loginStartPayload;" in start_block
    assert "const payloadSignature = loginStartPayloadSignature;" in start_block
    assert "body: JSON.stringify(payload)," in start_block
    stale_guard = "if (!isActiveLoginStart(targetAccountId, loginSeq, payloadSignature)) return;"
    assert stale_guard in start_block
    assert start_block.index(stale_guard) < start_block.index("setLoginFlow(flow);")
    assert start_block.index(stale_guard, start_block.index("catch")) < start_block.index("setError(error instanceof Error ? error.message : '启动备用授权登录失败');")
    assert "if (isActiveLoginSession(targetAccountId, loginSeq)) setLoginLoading(false);" in start_block
