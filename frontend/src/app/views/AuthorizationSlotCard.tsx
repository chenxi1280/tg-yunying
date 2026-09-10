import React from 'react';
import { Button, Card, Space, Tag, Typography } from 'antd';
import type { AccountAuthorizationAsset } from '../types';
import type { AuthorizationLoginForm } from './AuthorizationLoginModal';
import { StatusBadge } from '../components/shared';
import { formatBeijingDateTime } from '../time';

export const SWITCHABLE_STATUSES = new Set(['active', 'standby']);

export const roleLabel = (role: string) => {
  if (role === 'primary') return '主授权';
  if (role === 'standby_1') return '备用授权 1';
  if (role === 'standby_2') return '备用授权 2';
  if (role === 'standby_repair') return '待修复授权';
  return role;
};
const SLOT_SESSION_LABEL: Record<'primary' | 'standby_1' | 'standby_2', string> = {
  primary: 'primary session',
  standby_1: 'standby_1 session',
  standby_2: 'standby_2 session',
};

export const formatTime = (value: string | null | undefined) => value ? formatBeijingDateTime(value) : '暂无记录';
export const isMalaysiaWakeAsset = (asset: AccountAuthorizationAsset | undefined) => Boolean(
  asset && (asset.provision_region_code === 'my' || asset.credential_storage_scope === 'malaysia_wake_bundle'),
);

type Props = {
  role: 'primary' | 'standby_1' | 'standby_2';
  asset: AccountAuthorizationAsset | undefined;
  canManage: boolean;
  switchingId: number | null;
  refreshingId: number | null;
  confirmSwitch: (asset: AccountAuthorizationAsset) => void;
  confirmRefresh: (asset: AccountAuthorizationAsset) => void;
  setLoginForm: React.Dispatch<React.SetStateAction<AuthorizationLoginForm>>;
  openLoginModal: () => Promise<void>;
};

export function AuthorizationSlotCard({ role, asset, canManage, switchingId, refreshingId,
  confirmSwitch, confirmRefresh, setLoginForm, openLoginModal }: Props) {
    const isPrimary = role === 'primary';
    const malaysiaWakeAsset = isMalaysiaWakeAsset(asset);
    const canRecover = !isPrimary && !malaysiaWakeAsset && asset?.session_available && SWITCHABLE_STATUSES.has(asset.status);
    return (
      <Card key={role} size="small" className="summary-card">
        <Space direction="vertical" size={6}>
          <Space wrap>
            <Typography.Text strong>{SLOT_SESSION_LABEL[role]}</Typography.Text>
            <StatusBadge status={asset?.health_status || asset?.status || '缺失'} />
            {asset?.is_current && <Tag color="green">当前主授权</Tag>}
            {malaysiaWakeAsset && <Tag color="blue">马来西亚紧急唤起</Tag>}
          </Space>
          <Typography.Text type="secondary">开发者应用：{asset?.developer_app_id ? `App #${asset.developer_app_id} / api_id ${asset.developer_app_api_id || '未确认'}` : '未绑定'}</Typography.Text>
          <Typography.Text type="secondary">历史代理记录：{asset?.proxy_id ? `Proxy #${asset.proxy_id}` : '无'}</Typography.Text>
          <Typography.Text type="secondary">最近健康检查：{formatTime(asset?.last_health_check_at)}</Typography.Text>
          {asset && <Typography.Text type="secondary">区域 / 代：{asset.provision_region_code.toUpperCase()} / G{asset.slot_generation}</Typography.Text>}
          {asset && malaysiaWakeAsset && (
            <Typography.Text type="secondary">
              唤起包：{asset.recoverable_copy_count}/2 份；恢复密钥 {asset.kms_recovery_status}；恢复探测 {formatTime(asset.last_restore_probe_at)}
            </Typography.Text>
          )}
          <Typography.Text type={asset?.failure_reason ? 'danger' : 'secondary'}>{asset?.failure_reason || '验证码不可读取 / 2FA 未托管等故障槽位原因会显示在这里'}</Typography.Text>
          <Space wrap>
            {!isPrimary && !malaysiaWakeAsset && role !== 'standby_2' && <Button size="small" disabled={!canManage} onClick={() => { setLoginForm((current) => ({ ...current, role })); void openLoginModal(); }}>补齐</Button>}
            {!isPrimary && <Button size="small" disabled={!canRecover} loading={switchingId === asset?.id} onClick={() => asset && confirmSwitch(asset)}>激活恢复</Button>}
            {asset?.id && <Button size="small" disabled={!canManage} loading={refreshingId === asset.id} onClick={() => confirmRefresh(asset)}>刷新槽位</Button>}
          </Space>
        </Space>
      </Card>
    );
}
