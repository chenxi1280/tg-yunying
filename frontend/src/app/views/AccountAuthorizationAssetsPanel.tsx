import React from 'react';
import { Alert, Button, Card, Empty, Input, Modal, Select, Space, Table, Tag, Typography, message } from 'antd';
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
const SLOT_SESSION_LABEL: Record<'primary' | 'standby_1' | 'standby_2', string> = {
  primary: 'primary session',
  standby_1: 'standby_1 session',
  standby_2: 'standby_2 session',
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
  const [error, setError] = React.useState('');
  const [refreshError, setRefreshError] = React.useState('');
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
  const activeAccountId = React.useRef(accountId);
  const authorizationAssetsRequestRef = React.useRef({ accountId, seq: 0 });
  const loginSessionSeq = React.useRef(0);
  const latestLoginStartPayloadSignature = React.useRef('');
  const loginStartPayload = React.useMemo(() => ({
    method: loginForm.method,
    role: loginForm.role,
    developer_app_id: loginForm.developer_app_id,
    proxy_id: loginForm.proxy_id,
  }), [loginForm.developer_app_id, loginForm.method, loginForm.proxy_id, loginForm.role]);
  const loginStartPayloadSignature = React.useMemo(() => JSON.stringify(loginStartPayload), [loginStartPayload]);
  latestLoginStartPayloadSignature.current = loginStartPayloadSignature;

  React.useEffect(() => {
    activeAccountId.current = accountId;
    loginSessionSeq.current += 1;
    setAssets([]);
    setLoginOpen(false);
    setLoginFlow(null);
    setLoginLoading(false);
    setSwitchingId(null);
    setError('');
    setRefreshError('');
    void loadAssets(accountId);
  }, [accountId]);

  function isActiveAccount(targetAccountId: number) {
    return activeAccountId.current === targetAccountId;
  }

  function beginAuthorizationAssetsRequest(targetAccountId: number) {
    const requestSeq = authorizationAssetsRequestRef.current.seq + 1;
    authorizationAssetsRequestRef.current = { accountId: targetAccountId, seq: requestSeq };
    return requestSeq;
  }

  function isActiveAuthorizationAssetsRequest(targetAccountId: number, requestSeq: number) {
    return isActiveAccount(targetAccountId)
      && authorizationAssetsRequestRef.current.accountId === targetAccountId
      && authorizationAssetsRequestRef.current.seq === requestSeq;
  }

  function beginLoginSession() {
    loginSessionSeq.current += 1;
    return loginSessionSeq.current;
  }

  function currentLoginSession() {
    return loginSessionSeq.current;
  }

  function isActiveLoginSession(targetAccountId: number, loginSeq: number) {
    return isActiveAccount(targetAccountId) && loginSessionSeq.current === loginSeq;
  }

  function isActiveLoginStart(targetAccountId: number, loginSeq: number, payloadSignature: string) {
    return isActiveLoginSession(targetAccountId, loginSeq)
      && latestLoginStartPayloadSignature.current === payloadSignature;
  }

  async function refreshChangedAccount(targetAccountId: number, loginSeq?: number) {
    setRefreshError('');
    try {
      await onChanged();
    } catch (error) {
      const stillActive = loginSeq === undefined
        ? isActiveAccount(targetAccountId)
        : isActiveLoginSession(targetAccountId, loginSeq);
      if (!stillActive) return;
      setRefreshError(error instanceof Error ? error.message : String(error));
    }
  }

  async function loadAssets(targetAccountId = accountId): Promise<boolean> {
    const requestSeq = beginAuthorizationAssetsRequest(targetAccountId);
    setLoading(true);
    setError('');
    setRefreshError('');
    try {
      const nextAssets = await api<AccountAuthorizationAsset[]>(`/tg-accounts/${targetAccountId}/authorizations`);
      if (!isActiveAuthorizationAssetsRequest(targetAccountId, requestSeq)) return false;
      setAssets(nextAssets);
      return true;
    } catch (error) {
      if (!isActiveAuthorizationAssetsRequest(targetAccountId, requestSeq)) return false;
      setError(error instanceof Error ? error.message : '读取授权资产失败');
      return false;
    } finally {
      if (isActiveAuthorizationAssetsRequest(targetAccountId, requestSeq)) setLoading(false);
    }
  }

  async function openLoginModal() {
    const targetAccountId = accountId;
    const loginSeq = beginLoginSession();
    setLoginOpen(true);
    setLoginFlow(null);
    setLoginLoading(true);
    setError('');
    setRefreshError('');
    try {
      const [apps, proxyRows] = await Promise.all([
        api<DeveloperApp[]>('/developer-apps'),
        api<AccountProxy[]>('/account-proxies'),
      ]);
      if (!isActiveLoginSession(targetAccountId, loginSeq)) return;
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
    } catch (error) {
      if (!isActiveLoginSession(targetAccountId, loginSeq)) return;
      setError(error instanceof Error ? error.message : '读取备用授权登录资源失败');
    } finally {
      if (isActiveLoginSession(targetAccountId, loginSeq)) setLoginLoading(false);
    }
  }

  async function startStandbyLogin() {
    const targetAccountId = accountId;
    const loginSeq = currentLoginSession();
    const payload = loginStartPayload;
    const payloadSignature = loginStartPayloadSignature;
    setLoginLoading(true);
    setError('');
    setRefreshError('');
    try {
      const flow = await api<LoginFlow>(`/tg-accounts/${targetAccountId}/authorizations/login/start`, {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      if (!isActiveLoginStart(targetAccountId, loginSeq, payloadSignature)) return;
      setLoginFlow(flow);
      setLoginForm((current) => ({ ...current, code: flow.code_preview ?? '' }));
    } catch (error) {
      if (!isActiveLoginStart(targetAccountId, loginSeq, payloadSignature)) return;
      setError(error instanceof Error ? error.message : '启动备用授权登录失败');
    } finally {
      if (isActiveLoginSession(targetAccountId, loginSeq)) setLoginLoading(false);
    }
  }

  async function verifyStandbyLogin() {
    if (!loginFlow) return;
    const targetAccountId = accountId;
    const loginSeq = currentLoginSession();
    setLoginLoading(true);
    setError('');
    setRefreshError('');
    try {
      await api(`/tg-accounts/${targetAccountId}/authorizations/login/verify`, {
        method: 'POST',
        body: JSON.stringify({
          flow_id: loginFlow.id,
          code: loginForm.code.trim() || null,
          password_2fa: loginForm.password_2fa.trim() || null,
        }),
      });
      if (!isActiveLoginSession(targetAccountId, loginSeq)) return;
      await completeLoginModal(targetAccountId, loginSeq);
    } catch (error) {
      if (!isActiveLoginSession(targetAccountId, loginSeq)) return;
      setError(error instanceof Error ? error.message : '校验备用授权登录失败');
    } finally {
      if (isActiveLoginSession(targetAccountId, loginSeq)) setLoginLoading(false);
    }
  }

  async function checkQrLogin() {
    if (!loginFlow) return;
    const targetAccountId = accountId;
    const loginSeq = currentLoginSession();
    setLoginLoading(true);
    setError('');
    setRefreshError('');
    try {
      await api(`/tg-accounts/${targetAccountId}/authorizations/login/qr/check`, {
        method: 'POST',
        body: JSON.stringify({ flow_id: loginFlow.id }),
      });
      if (!isActiveLoginSession(targetAccountId, loginSeq)) return;
      await completeLoginModal(targetAccountId, loginSeq);
    } catch (error) {
      if (!isActiveLoginSession(targetAccountId, loginSeq)) return;
      setError(error instanceof Error ? error.message : '检查 QR 登录失败');
    } finally {
      if (isActiveLoginSession(targetAccountId, loginSeq)) setLoginLoading(false);
    }
  }

  async function completeLoginModal(targetAccountId: number, loginSeq: number) {
    if (!isActiveLoginSession(targetAccountId, loginSeq)) return;
    const loaded = await loadAssets(targetAccountId);
    if (!loaded || !isActiveLoginSession(targetAccountId, loginSeq)) return;
    await refreshChangedAccount(targetAccountId, loginSeq);
    if (!isActiveLoginSession(targetAccountId, loginSeq)) return;
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
    const targetAccountId = accountId;
    setSwitchingId(authorizationId);
    setError('');
    setRefreshError('');
    try {
      await api(`/tg-accounts/${targetAccountId}/authorizations/${authorizationId}/switch-primary`, {
        method: 'POST',
        body: JSON.stringify({ reason: '账号中心手动切换备用授权' }),
      });
      if (!isActiveAccount(targetAccountId)) return;
      const loaded = await loadAssets(targetAccountId);
      if (!loaded || !isActiveAccount(targetAccountId)) return;
      await refreshChangedAccount(targetAccountId);
      if (!isActiveAccount(targetAccountId)) return;
      void message.success('已切换主授权');
    } catch (error) {
      if (!isActiveAccount(targetAccountId)) return;
      setError(error instanceof Error ? error.message : '切换主授权失败');
    } finally {
      if (isActiveAccount(targetAccountId)) setSwitchingId(null);
    }
  }

  function closeLoginModal() {
    loginSessionSeq.current += 1;
    setLoginOpen(false);
    setLoginFlow(null);
    setLoginLoading(false);
  }

  function assetForRole(role: string) {
    return assets.find((asset) => asset.role === role);
  }

  function slotCard(role: 'primary' | 'standby_1' | 'standby_2') {
    const asset = assetForRole(role);
    const isPrimary = role === 'primary';
    const canRecover = !isPrimary && asset?.session_available && SWITCHABLE_STATUSES.has(asset.status);
    return (
      <Card key={role} size="small" className="summary-card">
        <Space direction="vertical" size={6}>
          <Space wrap>
            <Typography.Text strong>{SLOT_SESSION_LABEL[role]}</Typography.Text>
            <StatusBadge status={asset?.health_status || asset?.status || '缺失'} />
            {asset?.is_current && <Tag color="green">当前主授权</Tag>}
          </Space>
          <Typography.Text type="secondary">开发者应用：{asset?.developer_app_id ? `App #${asset.developer_app_id}` : '未绑定'}</Typography.Text>
          <Typography.Text type="secondary">代理：{asset?.proxy_id ? `Proxy #${asset.proxy_id}` : '未绑定'}</Typography.Text>
          <Typography.Text type="secondary">最近健康检查：{formatTime(asset?.last_health_check_at)}</Typography.Text>
          <Typography.Text type={asset?.failure_reason ? 'danger' : 'secondary'}>{asset?.failure_reason || '验证码不可读取 / 2FA 未托管 / 代理异常等故障槽位原因会显示在这里'}</Typography.Text>
          <Space wrap>
            {!isPrimary && <Button size="small" disabled={!canManage} onClick={() => { setLoginForm((current) => ({ ...current, role })); void openLoginModal(); }}>补齐</Button>}
            {!isPrimary && <Button size="small" disabled={!canRecover} loading={switchingId === asset?.id} onClick={() => asset && confirmSwitch(asset)}>激活恢复</Button>}
          </Space>
        </Space>
      </Card>
    );
  }

  const healthyStandbyCount = assets.filter((asset) => asset.role.startsWith('standby_') && asset.session_available && SWITCHABLE_STATUSES.has(asset.status)).length;
  const primaryAsset = assetForRole('primary');
  const recoveryStatus = primaryAsset?.session_available
    ? healthyStandbyCount >= 2 ? '完整一主两备' : `健康备用 session ${healthyStandbyCount}/2`
    : healthyStandbyCount > 0 ? '可从备用 session 激活恢复' : '主备均失效';

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
          <Button size="small" loading={loading} onClick={() => void loadAssets()}>刷新授权资产</Button>
        </Space>
      )}
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        {error && <Alert type="error" showIcon message={error} />}
        {refreshError && <Alert type="error" showIcon message="刷新账号授权资产失败" description={refreshError} />}
        <Alert
          type={healthyStandbyCount >= 2 && primaryAsset?.session_available ? 'success' : healthyStandbyCount > 0 ? 'warning' : 'error'}
          showIcon
          message={`恢复能力：${recoveryStatus}`}
          description="官方锚点设备状态来自登录设备清理预检；故障槽位会保留为待修复授权资产。"
        />
        <div className="summary-grid">
          {slotCard('primary')}
          {slotCard('standby_1')}
          {slotCard('standby_2')}
        </div>
      </Space>
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
        onCancel={closeLoginModal}
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
