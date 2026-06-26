from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RULES_CENTER_VIEW = PROJECT_ROOT / "frontend/src/app/views/RulesCenterView.tsx"
pytestmark = pytest.mark.no_postgres


def _source() -> str:
    return RULES_CENTER_VIEW.read_text()


def _function_body(source: str, function_name: str) -> str:
    start = source.index(f"async function {function_name}")
    candidates = [
        source.find("\n\n  async function", start + 1),
        source.find("\n\n  function", start + 1),
        source.find("\n\n  const ", start + 1),
    ]
    end = min(index for index in candidates if index != -1)
    return source[start:end]


def test_rules_center_actions_distinguish_refresh_failure_from_write_failure():
    source = _source()

    assert "async function fetchRulesCenterData(requestSeq: number)" in source
    assert "async function refreshRulesCenterAfterAction(actionLabel: string)" in source
    assert "规则中心数据刷新失败" in source
    assert "操作已完成" in source

    helper_start = source.index("async function refreshRulesCenterAfterAction")
    helper_end = source.index("\n\n  React.useEffect", helper_start)
    helper = source[helper_start:helper_end]
    assert "await fetchRulesCenterData(requestSeq);" in helper
    assert "setError(`规则中心数据刷新失败：" in helper

    for function_name in [
        "createRuleSet",
        "saveRuleSetConfig",
        "submitVersionAction",
    ]:
        body = _function_body(source, function_name)
        assert "await runRuleAction(" in body
        assert "await load();" not in body

    run_action = _function_body(source, "runRuleAction")
    assert "await refreshRulesCenterAfterAction(successText);" in run_action


def test_rules_center_data_refreshes_ignore_stale_responses():
    source = _source()

    fetch_data = _function_body(source, "fetchRulesCenterData")
    load_data = _function_body(source, "load")
    refresh_data = _function_body(source, "refreshRulesCenterAfterAction")

    assert "const rulesCenterDataRequestSeq = React.useRef(0);" in source
    assert "function beginRulesCenterDataRequest()" in source
    assert "rulesCenterDataRequestSeq.current += 1;" in source
    assert "function isActiveRulesCenterDataRequest(requestSeq: number)" in source
    assert "async function fetchRulesCenterData(requestSeq: number)" in source

    stale_guard = "if (!isActiveRulesCenterDataRequest(requestSeq)) return false;"
    assert stale_guard in fetch_data
    assert fetch_data.index(stale_guard) < fetch_data.index("setSummary(nextSummary);")
    assert "return true;" in fetch_data

    assert "const requestSeq = beginRulesCenterDataRequest();" in load_data
    assert "await fetchRulesCenterData(requestSeq);" in load_data
    load_error_guard = "if (!isActiveRulesCenterDataRequest(requestSeq)) return;"
    assert load_error_guard in load_data
    assert load_data.index(load_error_guard) < load_data.index("setError(err instanceof Error ? err.message : String(err));")
    assert "if (isActiveRulesCenterDataRequest(requestSeq)) setLoading(false);" in load_data

    assert "const requestSeq = beginRulesCenterDataRequest();" in refresh_data
    assert "await fetchRulesCenterData(requestSeq);" in refresh_data
    refresh_error_guard = "if (!isActiveRulesCenterDataRequest(requestSeq)) return;"
    assert refresh_error_guard in refresh_data
    assert refresh_data.index(refresh_error_guard) < refresh_data.index("setError(`规则中心数据刷新失败：")


def test_rules_center_rule_test_ignores_stale_payload_results():
    source = _source()
    run_rule_test = _function_body(source, "runRuleTest")

    assert "const activeRuleTestRequestSeq = React.useRef(0);" in source
    assert "const latestRuleTestPayloadSignature = React.useRef('');" in source
    assert "const ruleTestPayload = React.useMemo(() => ({" in source
    assert "const ruleTestPayloadSignature = React.useMemo(() => JSON.stringify(ruleTestPayload), [ruleTestPayload]);" in source
    assert "latestRuleTestPayloadSignature.current = ruleTestPayloadSignature;" in source
    assert "function beginRuleTestRequest()" in source
    assert "activeRuleTestRequestSeq.current += 1;" in source
    assert "function isCurrentRuleTestRequest(requestSeq: number)" in source
    assert "function isActiveRuleTestRequest(requestSeq: number, payloadSignature: string)" in source

    assert "const requestSeq = beginRuleTestRequest();" in run_rule_test
    assert "const payload = ruleTestPayload;" in run_rule_test
    assert "const payloadSignature = ruleTestPayloadSignature;" in run_rule_test
    assert "const result = await api<RuleTestResult>('/rules/test', {" in run_rule_test
    assert "body: JSON.stringify(payload)," in run_rule_test

    stale_guard = "if (!isActiveRuleTestRequest(requestSeq, payloadSignature)) return;"
    assert stale_guard in run_rule_test
    assert run_rule_test.index(stale_guard) < run_rule_test.index("setTestResult(result);")
    assert run_rule_test.index(stale_guard, run_rule_test.index("catch")) < run_rule_test.index("setError(err instanceof Error ? err.message : String(err));")
    assert "if (isCurrentRuleTestRequest(requestSeq)) setTesting(false);" in run_rule_test


def test_rules_center_write_actions_bind_action_key_payload_signature():
    source = _source()
    run_action = _function_body(source, "runRuleAction")
    create_rule_set = _function_body(source, "createRuleSet")
    save_config = _function_body(source, "saveRuleSetConfig")
    version_action = _function_body(source, "submitVersionAction")

    assert "const activeRuleActionRequestRef = React.useRef({ seq: 0, actionKey: '', signature: '' });" in source
    assert "function rulesCenterActionPayloadSignature(actionKey: string, payload: Record<string, unknown>)" in source
    assert "function beginRuleActionRequest(actionKey: string, signature: string)" in source
    assert "function isCurrentRuleActionRequest(requestSeq: number)" in source
    assert "function isActiveRuleActionRequest(" in source

    assert "async function runRuleAction(options: RuleActionRequestOptions, action: () => Promise<void>, successText: string)" in run_action
    assert "const requestSeq = beginRuleActionRequest(options.actionKey, options.payloadSignature);" in run_action
    assert "if (!isActiveRuleActionRequest(" in run_action
    assert run_action.index("if (!isActiveRuleActionRequest(") < run_action.index("void message.success(successText);")
    assert run_action.index("if (!isActiveRuleActionRequest(", run_action.index("catch")) < run_action.index("setError(err instanceof Error ? err.message : String(err));")
    assert "if (isCurrentRuleActionRequest(requestSeq)) setSaving(false);" in run_action

    assert "const actionKey = 'rule-set:create';" in create_rule_set
    assert "const payload = createRuleSetPayload(values);" in create_rule_set
    assert "currentPayloadSignature: () => latestCreateOpenRef.current" in create_rule_set
    assert "rulesCenterActionPayloadSignature(actionKey, createRuleSetPayload(createForm.getFieldsValue(true)))" in create_rule_set

    assert "const actionKey = `rule-set:${configTarget.id}:config`;".replace("`", "`") in save_config
    assert "const payload = ruleConfig(values);" in save_config
    assert "currentPayloadSignature: () => latestConfigTargetRef.current?.id === configTarget.id" in save_config
    assert "rulesCenterActionPayloadSignature(actionKey, ruleConfig(configForm.getFieldsValue(true)))" in save_config

    assert "const actionKey = `rule-set:${versionAction.ruleSet.id}:version:${versionAction.version.id}:${versionAction.action}`;" in version_action
    assert "const payload = { reason };" in version_action
    assert "const currentAction = latestVersionActionRef.current;" in version_action
    assert "currentAction?.ruleSet.id !== versionAction.ruleSet.id" in version_action
    assert "currentAction.version.id !== versionAction.version.id" in version_action
    assert "currentAction.action !== versionAction.action" in version_action
    assert "rulesCenterActionPayloadSignature(actionKey, { reason: latestVersionActionReasonRef.current.trim() })" in version_action
