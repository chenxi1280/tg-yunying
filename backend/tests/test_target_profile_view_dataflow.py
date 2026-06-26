from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TARGET_PROFILE_VIEW = PROJECT_ROOT / "frontend/src/app/views/TargetProfileView.tsx"
TARGET_PROFILE_VIEW_MODEL = PROJECT_ROOT / "frontend/src/app/views/targetProfileViewModel.ts"


def test_target_profile_actions_distinguish_refresh_failure_from_write_failure():
    source = TARGET_PROFILE_VIEW.read_text()

    assert "async function fetchTargetProfileData(requestSeq: number)" in source
    assert "async function refreshTargetProfileAfterAction(actionLabel: string)" in source
    assert "目标画像数据刷新失败" in source
    assert "操作已完成" in source

    helper = source[
        source.index("async function refreshTargetProfileAfterAction"):
        source.index("\n\n  React.useEffect", source.index("async function refreshTargetProfileAfterAction"))
    ]
    assert "await fetchTargetProfileData(requestSeq);" in helper
    assert "setError(`目标画像数据刷新失败：" in helper

    run_action = source[source.index("async function runAction"):source.index("\n\n  async function saveSelectedSources")]
    assert "await refreshTargetProfileAfterAction(successText);" in run_action
    assert "await load();" not in run_action


def test_target_profile_refreshes_ignore_stale_responses():
    source = TARGET_PROFILE_VIEW.read_text()
    fetch_data = source[source.index("async function fetchTargetProfileData"):source.index("\n\n  async function load")]
    load_block = source[source.index("async function load"):source.index("\n\n  async function refreshTargetProfileAfterAction")]
    refresh_block = source[
        source.index("async function refreshTargetProfileAfterAction"):
        source.index("\n\n  React.useEffect", source.index("async function refreshTargetProfileAfterAction"))
    ]

    assert "const activeProfileDataRequestSeq = React.useRef(0);" in source
    assert "function beginProfileDataRequest()" in source
    assert "activeProfileDataRequestSeq.current += 1;" in source
    assert "function isActiveProfileDataRequest(requestSeq: number)" in source

    assert "async function fetchTargetProfileData(requestSeq: number)" in source
    assert "if (!isActiveProfileDataRequest(requestSeq)) return false;" in fetch_data
    assert fetch_data.index("if (!isActiveProfileDataRequest(requestSeq)) return false;") < fetch_data.index("setProfile(profileResult);")
    assert "return true;" in fetch_data

    assert "const requestSeq = beginProfileDataRequest();" in load_block
    assert "await fetchTargetProfileData(requestSeq);" in load_block
    assert "if (!isActiveProfileDataRequest(requestSeq)) return;" in load_block
    assert load_block.index("if (!isActiveProfileDataRequest(requestSeq)) return;") < load_block.index("setError(errorMessage(err));")
    assert "if (isActiveProfileDataRequest(requestSeq)) setLoading(false);" in load_block

    assert "const requestSeq = beginProfileDataRequest();" in refresh_block
    assert "await fetchTargetProfileData(requestSeq);" in refresh_block
    assert "if (!isActiveProfileDataRequest(requestSeq)) return;" in refresh_block
    assert refresh_block.index("if (!isActiveProfileDataRequest(requestSeq)) return;") < refresh_block.index("setError(`目标画像数据刷新失败：")


def test_target_profile_write_actions_bind_action_key_payload_signature():
    source = TARGET_PROFILE_VIEW.read_text()
    run_action = source[source.index("async function runAction"):source.index("\n\n  async function saveSelectedSources")]
    begin_action = source[source.index("function beginProfileActionRequest"):source.index("\n\n  function isCurrentProfileActionRequest")]
    save_sources = source[source.index("async function saveSelectedSources"):source.index("\n\n  async function sourceRun")]
    save_rules = source[source.index("async function saveQualityRules"):source.index("\n\n  async function updateSampleStatus")]

    assert "const activeProfileActionRequestRef = React.useRef({ seq: 0, actionKey: '', signature: '' });" in source
    assert "function targetProfileActionPayloadSignature(actionKey: string, payload: Record<string, unknown>)" in source
    assert "function beginProfileActionRequest(actionKey: string, signature: string)" in source
    assert "function isCurrentProfileActionRequest(requestSeq: number)" in source
    assert "function isActiveProfileActionRequest(" in source
    assert "activeProfileDataRequestSeq.current += 1;" in begin_action

    assert "actionKey: string;" in source
    assert "payloadSignature: string;" in source
    assert "currentPayloadSignature: () => string;" in source
    assert "async function runAction(options: ProfileActionRequestOptions" in run_action
    assert "const requestSeq = beginProfileActionRequest(options.actionKey, options.payloadSignature);" in run_action
    assert "if (!isActiveProfileActionRequest(" in run_action
    assert run_action.index("if (!isActiveProfileActionRequest(") < run_action.index("void message.success(successText);")
    assert "setError(errorMessage(err));" in run_action
    assert run_action.index("if (!isActiveProfileActionRequest(", run_action.index("catch")) < run_action.index("setError(errorMessage(err));")
    assert "if (isCurrentProfileActionRequest(requestSeq)) setSaving(false);" in run_action

    assert "const actionKey = 'sources:save';" in save_sources
    assert "const payload = selectedSourcesPayload(reason);" in save_sources
    assert "currentPayloadSignature: () => targetProfileActionPayloadSignature(actionKey, selectedSourcesPayload(reason))" in save_sources

    assert "const actionKey = 'quality-rules:save';" in save_rules
    assert "const payload = formToRule(values, reason);" in save_rules
    assert "currentPayloadSignature: () => targetProfileActionPayloadSignature(actionKey, formToRule(form.getFieldsValue(true), reason))" in save_rules


def test_target_profile_recommended_sources_are_not_default_selected():
    source = TARGET_PROFILE_VIEW_MODEL.read_text()
    start = source.index("export function selectedSourceKeys")
    helper = source[start:source.index("\n}", start) + 2]

    assert "activeTargetIds" in helper
    assert "activeKeys" in helper
    assert "return activeKeys;" in helper
    assert "recommended && item.can_listen" not in helper
    assert helper.count("return ") == 1


def test_target_profile_quality_rule_form_covers_scene_weights_and_forbidden_mode():
    view = TARGET_PROFILE_VIEW.read_text()
    view_model = TARGET_PROFILE_VIEW_MODEL.read_text()

    for field in ["group_chat_weight", "channel_comment_weight", "discussion_reply_weight", "forbidden_mode"]:
        assert f"name=\"{field}\"" in view
        assert f"{field}:" in view_model

    assert "scene_weights: {" in view_model
    assert "group_chat: values.group_chat_weight" in view_model
    assert "channel_comment: values.channel_comment_weight" in view_model
    assert "discussion_reply: values.discussion_reply_weight" in view_model
    assert "mode: values.forbidden_mode" in view_model
    assert "rule?.scene_weights?.group_chat" in view_model
    assert "rule?.forbidden_patterns?.mode" in view_model


def test_target_profile_sample_governance_and_danger_actions_match_prd():
    source = TARGET_PROFILE_VIEW.read_text()
    sample_columns = source[source.index("const sampleColumns"):source.index("\n\n  const runColumns")]
    rebuild = source[source.index("async function rebuildProfile"):source.index("\n\n  async function clearProfile")]
    clear = source[source.index("async function clearProfile"):source.index("\n\n  async function updateLearningEnabled")]

    assert "updateSampleStatus(item, 'accepted')" in sample_columns
    assert "updateSampleStatus(item, 'downweighted')" in sample_columns
    assert "updateSampleStatus(item, 'rejected')" in sample_columns
    assert "填写样本状态调整原因" in source

    assert "confirmDangerAction(" in rebuild
    assert "confirmDangerAction(" in clear
    assert "确定重建全站目标画像？" in rebuild
    assert "确定清空全站目标画像？" in clear


def test_target_profile_page_exposes_recompute_candidate_action():
    source = TARGET_PROFILE_VIEW.read_text()

    assert "async function recomputeCandidates()" in source
    assert "填写重算候选样本的原因" in source
    assert "api('/target-profile/recompute-candidates'" in source
    assert "重算候选" in source
