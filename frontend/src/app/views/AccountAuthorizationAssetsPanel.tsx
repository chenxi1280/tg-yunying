import React from 'react';
import { Button, Card, Empty, Input, Modal, Select, Space, Table, Typography, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { AccountAuthorizationAsset, AccountProxy, DeveloperApp, LoginFlow } from '../types';
import { StatusBadge } from '../components/shared';
import { api } from '../../shared/api/client';
import { formatBeijingDateTime } from '../time';

const SWITCHABLE_STATUSES = new Set(['active', 'standby']);

const roleLabel = (role: string) => {
  if (role === 'primary') return '主授权';
  if (role === 'standby_1') return '备用授权 1';
  if (role === 'standby_2') return '备用授权 2';
  if (role === 'standby_repair') return '待修复授权';
  return role;
};

const formatTime = (value: string | null | undefined) => value ? formatBeijingDateTime(value) : '暂无记录';

export function AccountAuthorizationAssetsPanel({
  accountId,
  canManage,
  onChanged,
}: {
  accountId: number;
  canManage: boolean;
  onChanged: () => Promise<void>;
}) {
  const [assets, setAssets] = React.useState<AccountAuthorizationAsset[]>([]);
  const [loading, setLoading] = React.useState(false);
  const [switchingId, setSwitchingId] = React.useState<number | null>(null);
  const [loginOpen, setLoginOpen] = React.useState(false);
  const [loginLoading, setLoginLoading] = React.useState(false);
  const [developerApps, setDeveloperApps] = React.useState<DeveloperApp[]>([]);
  const [proxies, setProxies] = React.useState<AccountProxy[]>([]);
  const [loginFlow, setLoginFlow] = React.useState<LoginFlow | null>(null);
  const [loginForm, setLoginForm] = React.useState({
    role: 'standby_1',
    method: 'code',
    developer_app_id: 0,
    proxy_id: 0,
    code: '',
    password_2fa: '',
  });

  React.useEffect(() => {
    void loadAssets();
  }, [accountId]);

  async function loadAssets() {
    setLoading(true);
    try {
      setAssets(await api<AccountAuthorizationAsset[]>(`/tg-accounts/${accountId}/authorizations`));
    } finally {
      setLoading(false);
    }
  }

  async function openLoginModal() {
    setLoginOpen(true);
    setLoginFlow(null);
    setLoginLoading(true);
    try {
      const [apps, proxyRows] = await Promise.all([
        api<DeveloperApp[]>('/developer-apps'),
        api<AccountProxy[]>('/account-proxies'),
      ]);
      const firstApp = apps.find((app) => app.is_active && app.health_status === '健康') ?? apps[0];
      const firstProxy = proxyRows.find((proxy) => proxy.status === 'healthy' || proxy.status === '健康') ?? proxyRows[0];
      setDeveloperApps(apps);
      setProxies(proxyRows);
      setLoginForm((current) => ({
        ...current,
        developer_app_id: firstApp?.id ?? 0,
        proxy_id: firstProxy?.id ?? 0,
        code: '',
        password_2fa: '',
      }));
    } finally {
      setLoginLoading(false);
    }
  }

  async function startStandbyLogin() {
    setLoginLoading(true);
    try {
      const flow = await api<LoginFlow>(`/tg-accounts/${accountId}/authorizations/login/start`, {
        method: 'POST',
        body: JSON.stringify({
          method: loginForm.method,
          role: loginForm.role,
          developer_app_id: loginForm.developer_app_id,
          proxy_id: loginForm.proxy_id,
        }),
      });
      setLoginFlow(flow);
      setLoginForm((current) => ({ ...current, code: flow.code_preview ?? '' }));
    } finally {
      setLoginLoading(false);
    }
  }

  async function verifyStandbyLogin() {
    if (!loginFlow) return;
    setLoginLoading(true);
    try {
      await api(`/tg-accounts/${accountId}/authorizations/login/verify`, {
        method: 'POST',
        body: JSON.stringify({
          flow_id: loginFlow.id,
          code: loginForm.code.trim() || null,
          password_2fa: loginForm.password_2fa.trim() || null,
        }),
      });
      await completeLoginModal();
    } finally {
      setLoginLoading(false);
    }
  }

  async function checkQrLogin() {
    if (!loginFlow) return;
    setLoginLoading(true);
    try {
      await api(`/tg-accounts/${accountId}/authorizations/login/qr/check`, {
        method: 'POST',
        body: JSON.stringify({ flow_id: loginFlow.id }),
      });
      await completeLoginModal();
    } finally {
      setLoginLoading(false);
    }
  }

  async function completeLoginModal() {
    await loadAssets();
    await onChanged();
    setLoginOpen(false);
    setLoginFlow(null);
    void message.success('备用授权已登录');
  }

  function confirmSwitch(asset: AccountAuthorizationAsset) {
    const authorizationId = asset.id;
    if (!authorizationId) return;
    Modal.confirm({
      title: '切换主授权',
      content: `确认将 ${roleLabel(asset.role)} 切换为当前主授权？`,
      okText: '切换',
      cancelText: '取消',
      onOk: () => switchPrimary(authorizationId),
    });
  }

  async function switchPrimary(authorizationId: number) {
    setSwitchingId(authorizationId);
    try {
      await api(`/tg-accounts/${accountId}/authorizations/${authorizationId}/switch-primary`, {
        method: 'POST',
        body: JSON.stringify({ reason: '账号中心手动切换备用授权' }),
      });
      await loadAssets();
      await onChanged();
      void message.success('已切换主授权');
    } finally {
      setSwitchingId(null);
    }
  }

  const columns: ColumnsType<AccountAuthorizationAsset> = [
    {
      title: '授权角色',
      key: 'role',
      width: 150,
      render: (_, asset) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{roleLabel(asset.role)}</Typography.Text>
          <Typography.Text type="secondary">{asset.primary_source === 'legacy_account' ? '存量主授权' : `授权 #${asset.id}`}</Typography.Text>
        </Space>
      ),
    },
    {
      title: '状态',
      key: 'status',
      width: 150,
      render: (_, asset) => <StatusBadge status={asset.status} label={asset.is_current ? '当前主授权' : asset.status} />,
    },
    { title: 'Session', key: 'session', width: 120, render: (_, asset) => <StatusBadge status={asset.session_available ? '可用' : '缺失'} /> },
    {
      title: '开发者应用',
      dataIndex: 'developer_app_id',
      key: 'developer_app_id',
      width: 130,
      render: (value) => value ? `App #${value}` : '未绑定',
    },
    { title: '代理', dataIndex: 'proxy_id', key: 'proxy_id', width: 120, render: (value) => value ? `Proxy #${value}` : '未绑定' },
    { title: '最近切换', key: 'last_switched_at', width: 190, render: (_, asset) => formatTime(asset.last_switched_at) },
    {
      title: '操作',
      key: 'actions',
      width: 130,
      render: (_, asset) => {
        const canSwitch = canManage
          && Boolean(asset.id)
          && asset.role !== 'primary'
          && asset.session_available
          && SWITCHABLE_STATUSES.has(asset.status);
        return (
          <Button size="small" disabled={!canSwitch} loading={switchingId === asset.id} onClick={() => confirmSwitch(asset)}>
            切为主授权
          </Button>
        );
      },
    },
  ];

  return (
    <Card
      className="sub-panel compact-panel"
      title="授权资产"
      extra={(
        <Space>
          <Button size="small" disabled={!canManage} onClick={openLoginModal}>新增备用授权</Button>
          <Button size="small" loading={loading} onClick={loadAssets}>刷新授权资产</Button>
        </Space>
      )}
    >
      <Table<AccountAuthorizationAsset>
        className="tg-table"
        rowKey={(asset) => `${asset.primary_source}-${asset.id ?? 'legacy'}`}
        columns={columns}
        dataSource={assets}
        pagination={false}
        loading={loading}
        scroll={{ x: 1030 }}
        locale={{ emptyText: <Empty description="暂无授权资产" /> }}
      />
      <Modal
        title="新增备用授权"
        open={loginOpen}
        onCancel={() => setLoginOpen(false)}
        footer={null}
        destroyOnHidden
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Select
            value={loginForm.role}
            onChange={(role) => setLoginForm({ ...loginForm, role })}
            options={[
              { value: 'standby_1', label: '备用授权 1' },
              { value: 'standby_2', label: '备用授权 2' },
            ]}
          />
          <Select
            value={loginForm.developer_app_id || undefined}
            placeholder="选择开发者应用"
            onChange={(developer_app_id) => setLoginForm({ ...loginForm, developer_app_id })}
            options={developerApps.map((app) => ({ value: app.id, label: `${app.app_name} / ${app.health_status}` }))}
          />
          <Select
            value={loginForm.proxy_id || undefined}
            placeholder="选择代理"
            onChange={(proxy_id) => setLoginForm({ ...loginForm, proxy_id })}
            options={proxies.map((proxy) => ({ value: proxy.id, label: `${proxy.name} / ${proxy.status}` }))}
          />
          <Select
            value={loginForm.method}
            onChange={(method) => setLoginForm({ ...loginForm, method })}
            options={[{ value: 'code', label: '验证码登录' }, { value: 'qr', label: '扫码登录' }]}
          />
          {!loginFlow && (
            <Button
              type="primary"
              loading={loginLoading}
              disabled={!loginForm.developer_app_id || !loginForm.proxy_id}
              onClick={startStandbyLogin}
            >
              发起登录
            </Button>
          )}
          {loginFlow && loginForm.method === 'code' && (
            <>
              <Input
                value={loginForm.code}
                placeholder="验证码"
                onChange={(event) => setLoginForm({ ...loginForm, code: event.target.value })}
              />
              <Input.Password
                value={loginForm.password_2fa}
                placeholder="2FA 密码（如需要）"
                onChange={(event) => setLoginForm({ ...loginForm, password_2fa: event.target.value })}
              />
              <Button type="primary" loading={loginLoading} onClick={verifyStandbyLogin}>完成备用授权登录</Button>
            </>
          )}
          {loginFlow && loginForm.method === 'qr' && (
            <>
              <Typography.Paragraph copyable>{loginFlow.qr_payload || '等待二维码 payload'}</Typography.Paragraph>
              <Button type="primary" loading={loginLoading} onClick={checkQrLogin}>我已扫码，检查登录</Button>
            </>
          )}
        </Space>
      </Modal>
    </Card>
  );
}
