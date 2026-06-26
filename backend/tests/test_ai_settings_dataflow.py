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
