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
    assert "onPressEnter={verifyStandbyLogin}" in auth_panel


def test_clash_config_view_distinguishes_save_sync_and_health_states():
    source = (PROJECT_ROOT / "frontend/src/app/views/ProxyAirportSubscriptionView.tsx").read_text()

    assert "function proxyAirportReadinessLabel" in source
    assert "配置已保存，等待节点同步" in source
    assert "节点同步失败" in source
    assert "同步成功但健康节点为 0" in source
    assert "健康节点可用" in source
    assert "订阅节点已解析，健康探测完成前不可作为可用代理池" in source


def test_account_masks_view_shows_unobservable_missing_fields():
    source = (PROJECT_ROOT / "frontend/src/app/views/AccountMasksView.tsx").read_text()
    types = (PROJECT_ROOT / "frontend/src/app/types/system.ts").read_text()

    assert "observed_missing_fields: string[]" in types
    assert "function observedFingerprintText" in source
    assert "缺失字段：" in source
    assert "observed_missing_fields" in source
