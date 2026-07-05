from __future__ import annotations

from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.no_postgres


def test_account_login_modals_support_enter_submit():
    app_modals = (PROJECT_ROOT / "frontend/src/app/AppModals.tsx").read_text()
    auth_panel = (PROJECT_ROOT / "frontend/src/app/views/AccountAuthorizationAssetsPanel.tsx").read_text()

    assert "onPressEnter={submitAccountLoginCode}" in app_modals
    assert "onPressEnter={submitAccountLoginPassword}" in app_modals
    assert "onPressEnter={submitAccountCreate}" in app_modals
    assert "onPressEnter={verifyStandbyLogin}" in auth_panel


def test_clash_config_view_distinguishes_save_sync_and_health_states():
    source = (PROJECT_ROOT / "frontend/src/app/views/ProxyAirportSubscriptionView.tsx").read_text()
    types = (PROJECT_ROOT / "frontend/src/app/types/system.ts").read_text()

    assert "function proxyAirportReadinessLabel" in source
    assert "Clash 订阅源池" in source
    assert "api<ProxyAirportSubscription[]>('/proxy-airport-subscriptions')" in source
    assert "api<ProxyAirportSubscription>('/proxy-airport-subscriptions'" in source
    assert "`/proxy-airport-subscriptions/${editingRow.id}`" in source
    assert "`/proxy-airport-subscriptions/${row.id}/sync`" in source
    assert "留空则不修改已保存地址" in source
    assert "配置已保存，等待节点同步" in source
    assert "节点同步失败" in source
    assert "同步成功但健康节点为 0" in source
    assert "健康节点可用" in source
    assert "订阅节点已解析，健康探测完成前不可作为可用代理池" in source
    assert "dataIndex: 'priority'" in source
    assert "dataIndex: 'enabled'" in source
    assert "label: '同步节点数'" in source
    assert "label: '健康节点数'" in source
    assert "label: '最近同步时间'" in source
    assert "row.last_sync_at" in source
    assert "priority: number" in types
    assert "enabled: boolean" in types


def test_account_masks_view_shows_unobservable_missing_fields():
    source = (PROJECT_ROOT / "frontend/src/app/views/AccountMasksView.tsx").read_text()
    types = (PROJECT_ROOT / "frontend/src/app/types/system.ts").read_text()

    assert "observed_missing_fields: string[]" in types
    assert "function observedFingerprintText" in source
    assert "缺失字段：" in source
    assert "observed_missing_fields" in source


def test_account_masks_view_keys_rows_by_full_authorization_slot_and_shows_authorization_id():
    source = (PROJECT_ROOT / "frontend/src/app/views/AccountMasksView.tsx").read_text()

    assert "rowKey={(row) => accountEnvironmentRowKey(row)}" in source
    assert "function accountEnvironmentRowKey(row: AccountEnvironmentBinding)" in source
    for field in ["account_id", "developer_app_id", "developer_app_api_id_snapshot", "authorization_id", "session_role"]:
        assert f"row.{field}" in source
    assert "{ title: '授权ID', dataIndex: 'authorization_id' }" in source
