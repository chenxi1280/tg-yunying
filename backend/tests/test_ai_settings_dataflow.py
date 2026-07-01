from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_ai_settings_cache_config_save_distinguishes_refresh_failure():
    source = (PROJECT_ROOT / "frontend/src/app/views/AISettingsView.tsx").read_text()
    save_block = source[source.index("async function saveMaterialCacheConfig"):source.index("\n\n  return (")]

    assert "const [cacheConfigRefreshError, setCacheConfigRefreshError] = React.useState('');" in source
    assert 'message="缓存配置刷新失败"' in source
    assert "setCacheConfigRefreshError('');" in save_block
    assert "let saved: MaterialCacheConfig;" in save_block
    assert "saved = await api<MaterialCacheConfig>('/materials/cache/config'" in save_block
    assert "setCacheConfigError(error instanceof Error ? error.message : '保存缓存配置失败');" in save_block
    assert "try {\n        await onSavedMaterialCacheConfig();" in save_block
    assert "setCacheConfigRefreshError(error instanceof Error ? error.message : String(error));" in save_block

    refresh_failure_block = save_block[save_block.index("await onSavedMaterialCacheConfig()"):]
    assert "setCacheConfigError(" not in refresh_failure_block


def test_ai_settings_cache_config_save_binds_payload_signature_and_request_seq():
    source = (PROJECT_ROOT / "frontend/src/app/views/AISettingsView.tsx").read_text()
    save_block = source[source.index("async function saveMaterialCacheConfig"):source.index("\n\n  return (")]

    assert "const activeCacheConfigSaveRequestRef = React.useRef({ seq: 0, signature: '' });" in source
    assert "function materialCacheConfigPayloadSignature(values: MaterialCacheConfigFormValues)" in source
    assert "function beginCacheConfigSaveRequest(signature: string)" in source
    assert "function currentMaterialCacheConfigPayloadSignature()" in source
    assert "function isActiveCacheConfigSaveRequest(request: { seq: number; signature: string })" in source
    assert "function isCurrentCacheConfigSaveRequest(request: { seq: number; signature: string })" in source

    assert "const saveRequest = beginCacheConfigSaveRequest(materialCacheConfigPayloadSignature(values));" in save_block
    assert "if (!isCurrentCacheConfigSaveRequest(saveRequest)) return;" in save_block
    assert save_block.index("if (!isCurrentCacheConfigSaveRequest(saveRequest)) return;") > save_block.index("saved = await api<MaterialCacheConfig>('/materials/cache/config'")
    assert save_block.index("if (!isCurrentCacheConfigSaveRequest(saveRequest)) return;") < save_block.index("await onSavedMaterialCacheConfig();")
    assert "if (!isCurrentCacheConfigSaveRequest(saveRequest)) return;" in save_block[save_block.index("} catch (error) {"):]
    assert "if (isActiveCacheConfigSaveRequest(saveRequest)) setSavingCacheConfig(false);" in save_block


def test_ai_settings_cache_account_selector_is_searchable_by_label():
    source = (PROJECT_ROOT / "frontend/src/app/views/AISettingsView.tsx").read_text()
    account_selector = source[source.index('name="material_cache_account_id"'):source.index("\n              </Form.Item>", source.index('name="material_cache_account_id"'))]

    assert "showSearch" in account_selector
    assert "optionFilterProp=\"label\"" in account_selector
    assert "placeholder=\"按手机号 / 备注名 / username 搜索缓存执行账号\"" in account_selector


def test_ai_account_voice_profile_routes_exist_and_require_real_service():
    router = (PROJECT_ROOT / "backend/app/api/routers/ai_config.py").read_text()
    schemas = (PROJECT_ROOT / "backend/app/schemas/ai_config.py").read_text()

    assert '"/api/ai-account-voice-profiles"' in router
    assert '"/api/ai-account-voice-profiles/{account_id}"' in router
    assert '"/api/ai-account-voice-profiles/{account_id}/rebuild"' in router
    assert '"/api/ai-account-voice-profiles/{account_id}/versions"' in router
    assert '"/api/ai-account-voice-profiles/{account_id}/audits"' in router
    assert '"/api/ai-account-voice-profiles/{account_id}/rollback"' in router
    assert '"/api/ai-account-voice-profiles/batch-rebuild"' in router
    assert '"/api/ai-account-voice-profiles/batch-status"' in router
    assert "list_voice_profiles(" in router
    assert "patch_voice_profile(" in router
    assert "rebuild_voice_profile(" in router
    assert "list_voice_profile_versions(" in router
    assert "list_voice_profile_audits(" in router
    assert "rollback_voice_profile(" in router
    assert "batch_rebuild_voice_profiles(" in router
    assert "batch_update_voice_profile_status(" in router
    assert "generate_voice_profiles_with_ai(" in router
    assert "AiAccountVoiceProfileOut" in schemas
    assert "AiAccountVoiceProfileVersionOut" in schemas
    assert "AiAccountVoiceProfileAuditOut" in schemas
    assert "AiAccountVoiceProfileRollbackRequest" in schemas
    assert "AiAccountVoiceProfileUpdate" in schemas
    assert "AiAccountVoiceProfileBatchRebuildRequest" in schemas
    assert "AiAccountVoiceProfileBatchItemOut" in schemas
    assert "AiAccountVoiceProfileBatchStatusRequest" in schemas
    assert "AiAccountVoiceProfileBatchStatusOut" in schemas


def test_system_config_exposes_ai_account_voice_profile_management_tab():
    system_view = (PROJECT_ROOT / "frontend/src/app/views/SystemConfigView.tsx").read_text()
    profile_view = (PROJECT_ROOT / "frontend/src/app/views/AIAccountVoiceProfilesView.tsx").read_text()
    system_types = (PROJECT_ROOT / "frontend/src/app/types/system.ts").read_text()

    assert "import AIAccountVoiceProfilesView from './AIAccountVoiceProfilesView';" in system_view
    assert "key: 'ai-voice-profiles'" in system_view
    assert "label: '账号面具'" in system_view
    assert "hasPermission(currentUser, 'ai_voice_profiles.manage')" in system_view
    assert "<AIAccountVoiceProfilesView" in system_view

    assert "export type AiAccountVoiceProfile" in system_types
    assert "mask_name: string;" in system_types
    assert "audience_archetype: string;" in system_types
    assert "identity_frame: string;" in system_types
    assert "preference_tags: string[];" in system_types
    assert "export type AiAccountVoiceProfileBatchResultItem" in system_types
    assert "export type AiAccountVoiceProfileVersion" in system_types
    assert "export type AiAccountVoiceProfileAudit" in system_types
    assert "api<AiAccountVoiceProfile[]>('/ai-account-voice-profiles" in profile_view
    assert "api<AiAccountVoiceProfile>(`/ai-account-voice-profiles/${profile.account_id}`" in profile_view
    assert "api<AiAccountVoiceProfile>(`/ai-account-voice-profiles/${profile.account_id}/rebuild`" in profile_view
    assert "api<AiAccountVoiceProfileVersion[]>(`/ai-account-voice-profiles/${profile.account_id}/versions`" in profile_view
    assert "api<AiAccountVoiceProfileAudit[]>(`/ai-account-voice-profiles/${profile.account_id}/audits`" in profile_view
    assert "api<AiAccountVoiceProfile>(`/ai-account-voice-profiles/${profile.account_id}/rollback`" in profile_view
    assert "api<AiAccountVoiceProfileBatchRebuildOut>('/ai-account-voice-profiles/batch-rebuild'" in profile_view
    assert "missing_only: false" in profile_view
    assert "api<AiAccountVoiceProfileBatchStatusOut>('/ai-account-voice-profiles/batch-status'" in profile_view
    assert "批量生成结果" in profile_view
    assert "title: '失败原因'" in profile_view
    assert "title: '跳过原因'" in profile_view
    assert "缺面具" in profile_view
    assert "批量补齐缺面具账号" in profile_view
    assert "面具名称" in profile_view
    assert "人群设定" in profile_view
    assert "身份框架" in profile_view
    assert "偏好标签" in profile_view
    assert "批量重建" in profile_view
    assert "批量停用" in profile_view
    assert "批量恢复" in profile_view
    assert "版本历史" in profile_view
    assert "回滚到此版本" in profile_view


def test_tenant_ai_modal_uses_selected_model_token_limit():
    app_modals = (PROJECT_ROOT / "frontend/src/app/AppModals.tsx").read_text()
    modal_block = app_modals[app_modals.index("modal?.type === 'tenantAiEdit'"):app_modals.index("modal?.type === 'changePassword'")]

    assert "function tenantAiMaxTokensLimit" in app_modals
    assert "const DEFAULT_AI_MAX_TOKENS_LIMIT = 100000;" in app_modals
    assert "const MINIMAX_AI_MAX_TOKENS_LIMIT = 250000;" in app_modals
    assert "const selectedAiProvider = aiProviders.find((provider) => provider.id === selectedAiProviderId);" in app_modals
    assert "max={tenantAiMaxTokensLimit(selectedAiProvider)}" in modal_block
    assert "max={8192}" not in modal_block
