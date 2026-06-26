from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RISK_CONTROL_VIEW = PROJECT_ROOT / "frontend/src/app/views/RiskControlView.tsx"


def _risk_control_source() -> str:
    return RISK_CONTROL_VIEW.read_text()


def _function_body(source: str, function_name: str) -> str:
    start = source.index(f"async function {function_name}")
    candidates = [
        source.find("\n\n  async function", start + 1),
        source.find("\n\n  function", start + 1),
        source.find("\n\n  const ", start + 1),
    ]
    end = min(index for index in candidates if index != -1)
    return source[start:end]


def test_risk_control_actions_do_not_report_refresh_failure_as_write_failure():
    source = _risk_control_source()

    assert "function errorText(exc: unknown)" in source
    assert "async function refreshRiskControlSummaryAfterAction(actionLabel: string)" in source
    assert "风控中心数据刷新失败" in source
    assert "操作已完成" in source

    helper_start = source.index("async function refreshRiskControlSummaryAfterAction")
    helper_end = source.index("\n\n  async function checkProxy", helper_start)
    helper = source[helper_start:helper_end]
    assert "await fetchRiskControlData(requestSeq);" in helper
    assert "setError(`风控中心数据刷新失败：" in helper

    for function_name in [
        "checkProxy",
        "savePolicy",
        "saveProxy",
        "disableProxy",
        "handleProxyAlert",
        "submitProxyAlertIgnore",
    ]:
        body = _function_body(source, function_name)
        assert "await refreshRiskControlSummaryAfterAction(" in body
        assert "await loadSummary();" not in body


def test_risk_control_refreshes_ignore_stale_responses():
    source = _risk_control_source()
    fetch_data = source[source.index("const fetchRiskControlData"):source.index("\n\n  const loadSummary")]
    load_summary = source[source.index("const loadSummary"):source.index("\n\n  React.useEffect")]
    refresh_helper = source[
        source.index("async function refreshRiskControlSummaryAfterAction"):
        source.index("\n\n  async function checkProxy", source.index("async function refreshRiskControlSummaryAfterAction"))
    ]

    assert "const riskControlDataRequestSeq = React.useRef(0);" in source
    assert "const beginRiskControlDataRequest = React.useCallback(() => {" in source
    assert "riskControlDataRequestSeq.current += 1;" in source
    assert "const isActiveRiskControlDataRequest = React.useCallback((requestSeq: number)" in source

    assert "const fetchRiskControlData = React.useCallback(async (requestSeq: number)" in fetch_data
    assert "if (!isActiveRiskControlDataRequest(requestSeq)) return false;" in fetch_data
    assert fetch_data.index("if (!isActiveRiskControlDataRequest(requestSeq)) return false;") < fetch_data.index("setSummary(nextSummary);")
    assert "return true;" in fetch_data

    assert "const requestSeq = beginRiskControlDataRequest();" in load_summary
    assert "await fetchRiskControlData(requestSeq);" in load_summary
    assert "if (!isActiveRiskControlDataRequest(requestSeq)) return;" in load_summary
    assert load_summary.index("if (!isActiveRiskControlDataRequest(requestSeq)) return;") < load_summary.index("setError(exc instanceof Error ? exc.message : '读取风控中心失败');")
    assert "if (isActiveRiskControlDataRequest(requestSeq)) setLoading(false);" in load_summary

    assert "const requestSeq = beginRiskControlDataRequest();" in refresh_helper
    assert "await fetchRiskControlData(requestSeq);" in refresh_helper
    assert "if (!isActiveRiskControlDataRequest(requestSeq)) return;" in refresh_helper
    assert refresh_helper.index("if (!isActiveRiskControlDataRequest(requestSeq)) return;") < refresh_helper.index("setError(`风控中心数据刷新失败：")


def test_risk_control_actions_ignore_stale_action_responses():
    source = _risk_control_source()
    check_proxy = _function_body(source, "checkProxy")
    handle_alert = _function_body(source, "handleProxyAlert")
    ignore_alert = _function_body(source, "submitProxyAlertIgnore")

    assert "const activeRiskControlActionKey = React.useRef('');" in source
    assert "function beginRiskControlAction(actionKey: string)" in source
    assert "activeRiskControlActionKey.current = actionKey;" in source
    assert "function isActiveRiskControlAction(actionKey: string)" in source

    assert "const actionKey = beginRiskControlAction(`proxy-check:${proxyId}`);" in check_proxy
    assert "if (!isActiveRiskControlAction(actionKey)) return;" in check_proxy
    assert check_proxy.index("if (!isActiveRiskControlAction(actionKey)) return;") > check_proxy.index("await api(`/account-proxies/${proxyId}/check`")
    assert "if (isActiveRiskControlAction(actionKey)) setCheckingProxyId(null);" in check_proxy
    assert "if (!isActiveRiskControlAction(actionKey)) return;" in check_proxy[check_proxy.index("} catch (exc) {"):]

    for block in [handle_alert, ignore_alert]:
        assert "beginRiskControlAction(key);" in block
        assert "if (!isActiveRiskControlAction(key)) return;" in block
        assert block.index("if (!isActiveRiskControlAction(key)) return;") > block.index("await api(")
        assert "if (isActiveRiskControlAction(key)) setHandlingAction('');" in block
        assert "if (!isActiveRiskControlAction(key)) return;" in block[block.index("} catch (exc) {"):]


def test_risk_control_save_modals_bind_payload_signature_and_request_seq():
    source = _risk_control_source()
    save_policy = _function_body(source, "savePolicy")
    save_proxy = _function_body(source, "saveProxy")

    assert "const activeRiskPolicySaveRequestRef = React.useRef({ seq: 0, signature: '' });" in source
    assert "const activeRiskProxySaveRequestRef = React.useRef({ seq: 0, signature: '' });" in source
    assert "function riskPolicyPayloadSignature(payload: RiskGlobalPolicy)" in source
    assert "function riskProxyPayloadSignature(payload: ProxyFormValues)" in source
    assert "function beginRiskPolicySaveRequest(signature: string)" in source
    assert "function beginRiskProxySaveRequest(signature: string)" in source
    assert "function currentRiskPolicyPayloadSignature()" in source
    assert "function currentRiskProxyPayloadSignature()" in source
    assert "function isCurrentRiskPolicySaveRequest(request: { seq: number; signature: string })" in source
    assert "function isCurrentRiskProxySaveRequest(request: { seq: number; signature: string })" in source
    assert "function isActiveRiskPolicySaveRequest(request: { seq: number; signature: string })" in source
    assert "function isActiveRiskProxySaveRequest(request: { seq: number; signature: string })" in source

    assert "const signature = riskPolicyPayloadSignature(values);" in save_policy
    assert "const saveRequest = beginRiskPolicySaveRequest(signature);" in save_policy
    assert "if (!isCurrentRiskPolicySaveRequest(saveRequest)) return;" in save_policy
    assert save_policy.index("if (!isCurrentRiskPolicySaveRequest(saveRequest)) return;") > save_policy.index("await api('/risk-control/global-policy'")
    assert save_policy.index("if (!isCurrentRiskPolicySaveRequest(saveRequest)) return;") < save_policy.index("setPolicyOpen(false);")
    assert "if (!isCurrentRiskPolicySaveRequest(saveRequest)) return;" in save_policy[save_policy.index("} catch (exc) {"):]
    assert "if (isActiveRiskPolicySaveRequest(saveRequest)) setPolicySaving(false);" in save_policy

    assert "const signature = riskProxyPayloadSignature(values);" in save_proxy
    assert "const saveRequest = beginRiskProxySaveRequest(signature);" in save_proxy
    assert "if (!isCurrentRiskProxySaveRequest(saveRequest)) return;" in save_proxy
    assert save_proxy.index("if (!isCurrentRiskProxySaveRequest(saveRequest)) return;") > save_proxy.index("await api(isEdit ?")
    assert save_proxy.index("if (!isCurrentRiskProxySaveRequest(saveRequest)) return;") < save_proxy.index("setProxyOpen(false);")
    assert "if (!isCurrentRiskProxySaveRequest(saveRequest)) return;" in save_proxy[save_proxy.index("} catch (exc) {"):]
    assert "if (isActiveRiskProxySaveRequest(saveRequest)) setProxySaving(false);" in save_proxy
