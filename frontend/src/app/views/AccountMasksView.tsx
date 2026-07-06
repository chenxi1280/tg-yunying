import React from 'react';
import { Alert, Button, Descriptions, Form, Input, InputNumber, Modal, Select, Space, Table, Tabs, Tag } from 'antd';
import { api } from '../../shared/api/client';
import type { AccountEnvironmentBinding, AccountEnvironmentProxyBatchBindResult, AccountPool, AccountProxy, CurrentUser, ProxyAirportNode } from '../types';
import { hasPermission } from '../utils';
import AIAccountVoiceProfilesView from './AIAccountVoiceProfilesView';

interface Props {
  currentUser: CurrentUser | null;
}

type EnvironmentDraft = Pick<
  AccountEnvironmentBinding,
  | 'developer_app_id'
  | 'authorization_id'
  | 'session_role'
  | 'proxy_id'
  | 'device_model'
  | 'system_version'
  | 'app_version'
  | 'platform'
  | 'lang_code'
  | 'system_lang_code'
  | 'lang_pack'
  | 'region_code'
  | 'client_identity_key'
>;

type BatchProxyBindingDraft = {
  account_pool_id?: number;
  proxy_id?: number;
  proxy_airport_node_id?: number;
  session_role: string;
  change_reason: string;
};

function errorText(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}

function statusTag(status: string) {
  if (status === 'pending_effect') return <Tag color="gold">待下次生效</Tag>;
  if (status === 'observed_matched') return <Tag color="green">已匹配</Tag>;
  if (status === 'observed_mismatch') return <Tag color="red">不一致</Tag>;
  if (status === 'unobservable') return <Tag color="orange">不可观测</Tag>;
  return <Tag>{status || '未连接'}</Tag>;
}

function observedFingerprintText(row: AccountEnvironmentBinding) {
  const observed = [row.observed_device_model, row.observed_system_version, row.observed_app_version].filter(Boolean).join(' / ');
  const missing = row.observed_missing_fields?.length ? `缺失字段：${row.observed_missing_fields.join(', ')}` : '';
  return [observed, missing].filter(Boolean).join('；') || '-';
}

function accountEnvironmentRowKey(row: AccountEnvironmentBinding) {
  return [
    row.account_id,
    row.developer_app_id || '-',
    row.developer_app_api_id_snapshot || 0,
    row.authorization_id || '-',
    row.session_role,
  ].join(':');
}

function draftFromRow(row: AccountEnvironmentBinding): EnvironmentDraft {
  return {
    developer_app_id: row.developer_app_id,
    authorization_id: row.authorization_id,
    session_role: row.session_role,
    proxy_id: row.proxy_id,
    device_model: row.device_model || 'iPhone 15',
    system_version: row.system_version || 'iOS 17.5',
    app_version: row.app_version || '10.14.1',
    platform: row.platform || 'ios',
    lang_code: row.lang_code || 'zh',
    system_lang_code: row.system_lang_code || 'zh-CN',
    lang_pack: row.lang_pack || '',
    region_code: row.region_code || 'CN',
    client_identity_key: row.client_identity_key || `manual-${row.account_id}-${row.authorization_id}`,
  };
}

function proxyAirportNodeLabel(node: ProxyAirportNode) {
  const exit = node.observed_exit_ip ? ` / ${node.observed_exit_ip}` : '';
  return `${node.node_name} / ${node.protocol}://${node.proxy_host}:${node.proxy_port}${exit}`;
}

function BatchProxyBindingPanel({ canManageEnvironment, onBound }: { canManageEnvironment: boolean; onBound: () => void }) {
  const [accountPools, setAccountPools] = React.useState<AccountPool[]>([]);
  const [proxies, setProxies] = React.useState<AccountProxy[]>([]);
  const [airportNodes, setAirportNodes] = React.useState<ProxyAirportNode[]>([]);
  const [proxySource, setProxySource] = React.useState<'account_proxy' | 'airport_node'>('account_proxy');
  const [draft, setDraft] = React.useState<BatchProxyBindingDraft>({ session_role: 'primary', change_reason: '' });
  const [saving, setSaving] = React.useState(false);
  const [notice, setNotice] = React.useState('');
  const [error, setError] = React.useState('');

  React.useEffect(() => {
    void loadOptions();
  }, []);

  async function loadOptions() {
    const [poolResult, proxyResult, airportResult] = await Promise.allSettled([
      api<AccountPool[]>('/account-pools'),
      api<AccountProxy[]>('/account-proxies'),
      api<ProxyAirportNode[]>('/account-environment-bindings/proxy-airport-nodes'),
    ]);
    if (poolResult.status === 'fulfilled') {
      setAccountPools(poolResult.value.filter((pool) => pool.pool_purpose !== 'code_receiver' && pool.system_key !== 'code_receiver'));
    }
    if (proxyResult.status === 'fulfilled') setProxies(proxyResult.value);
    if (airportResult.status === 'fulfilled') setAirportNodes(airportResult.value);
    const optionErrors = [
      poolResult.status === 'rejected' ? `账号中心分组选项加载失败：${errorText(poolResult.reason)}` : '',
      proxyResult.status === 'rejected' ? `本地代理选项加载失败：${errorText(proxyResult.reason)}` : '',
      airportResult.status === 'rejected' ? `Clash 节点选项加载失败：${errorText(airportResult.reason)}` : '',
    ].filter(Boolean);
    setError(optionErrors.join('；'));
  }

  async function submitBatchProxyBinding() {
    setSaving(true);
    setError('');
    setNotice('');
    try {
      const payload =
        proxySource === 'airport_node'
          ? { account_pool_id: draft.account_pool_id, proxy_airport_node_id: draft.proxy_airport_node_id, session_role: draft.session_role, change_reason: draft.change_reason }
          : { account_pool_id: draft.account_pool_id, proxy_id: draft.proxy_id, session_role: draft.session_role, change_reason: draft.change_reason };
      const result = await api<AccountEnvironmentProxyBatchBindResult>('/account-environment-bindings/batch-proxy-bind', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      setNotice(`账号分组批量绑定代理完成：成功 ${result.success_count}，跳过 ${result.failed_count}`);
      onBound();
    } catch (submitError) {
      setError(errorText(submitError));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Space direction="vertical" style={{ width: '100%', marginBottom: 12 }}>
      {error && <Alert type="error" showIcon message={error} />}
      {notice && <Alert type="success" showIcon message={notice} />}
      <Space wrap>
        <Select
          placeholder="选择账号中心分组"
          value={draft.account_pool_id}
          onChange={(value) => setDraft((current) => ({ ...current, account_pool_id: value }))}
          style={{ width: 180 }}
          options={accountPools.map((pool) => ({ value: pool.id, label: `${pool.name} (${pool.account_count})` }))}
        />
        <Select
          value={proxySource}
          onChange={(value) => {
            setProxySource(value);
            setDraft((current) => ({ ...current, proxy_id: undefined, proxy_airport_node_id: undefined }));
          }}
          style={{ width: 128 }}
          options={[
            { value: 'account_proxy', label: '本地代理' },
            { value: 'airport_node', label: 'Clash 节点' },
          ]}
        />
        {proxySource === 'airport_node' ? (
          <Select
            placeholder="选择 Clash 节点"
            value={draft.proxy_airport_node_id}
            onChange={(value) => setDraft((current) => ({ ...current, proxy_airport_node_id: value, proxy_id: undefined }))}
            style={{ width: 260 }}
            options={airportNodes.map((node) => ({ value: node.id, label: proxyAirportNodeLabel(node) }))}
          />
        ) : (
          <Select
            placeholder="选择代理"
            value={draft.proxy_id}
            onChange={(value) => setDraft((current) => ({ ...current, proxy_id: value, proxy_airport_node_id: undefined }))}
            style={{ width: 180 }}
            options={proxies.map((proxy) => ({ value: proxy.id, label: `${proxy.name} / ${proxy.status}` }))}
          />
        )}
        <Select
          value={draft.session_role}
          onChange={(value) => setDraft((current) => ({ ...current, session_role: value }))}
          style={{ width: 128 }}
          options={[
            { value: 'primary', label: 'primary' },
            { value: 'standby_1', label: 'standby_1' },
            { value: 'standby_2', label: 'standby_2' },
          ]}
        />
        <Input
          placeholder="变更原因"
          value={draft.change_reason}
          onChange={(event) => setDraft((current) => ({ ...current, change_reason: event.target.value }))}
          style={{ width: 220 }}
        />
        <Button
          type="primary"
          disabled={!canManageEnvironment || !draft.account_pool_id || !(proxySource === 'airport_node' ? draft.proxy_airport_node_id : draft.proxy_id) || !draft.change_reason.trim()}
          loading={saving}
          onClick={() => void submitBatchProxyBinding()}
        >
          账号分组批量绑定代理
        </Button>
      </Space>
      <Alert type="info" showIcon message="只更新已有授权环境；系统设置 Clash 配置不启用代理，也不按分组分配账号。" />
    </Space>
  );
}

export default function AccountMasksView({ currentUser }: Props) {
  const canManageVoiceProfiles = hasPermission(currentUser, 'ai_voice_profiles.manage');
  const canManageEnvironment = hasPermission(currentUser, 'account_environment.manage');
  const [rows, setRows] = React.useState<AccountEnvironmentBinding[]>([]);
  const [search, setSearch] = React.useState('');
  const [loading, setLoading] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState('');
  const [editing, setEditing] = React.useState<AccountEnvironmentBinding | null>(null);
  const [draft, setDraft] = React.useState<EnvironmentDraft | null>(null);

  React.useEffect(() => {
    void loadEnvironment();
  }, []);

  async function loadEnvironment(nextSearch = search) {
    const params = new URLSearchParams();
    if (nextSearch.trim()) params.set('search', nextSearch.trim());
    setLoading(true);
    setError('');
    try {
      setRows(await api<AccountEnvironmentBinding[]>('/account-environment-bindings?' + params.toString()));
    } catch (loadError) {
      setError(errorText(loadError));
    } finally {
      setLoading(false);
    }
  }

  function openEdit(row: AccountEnvironmentBinding) {
    setEditing(row);
    setDraft(draftFromRow(row));
    setError('');
  }

  function updateDraft(field: keyof EnvironmentDraft, value: string | number | null) {
    setDraft((current) => current ? { ...current, [field]: value } : current);
  }

  async function saveEnvironment() {
    if (!editing || !draft) return;
    setSaving(true);
    setError('');
    try {
      const updated = await api<AccountEnvironmentBinding>(`/account-environment-bindings/${editing.account_id}`, {
        method: 'PATCH',
        body: JSON.stringify(draft),
      });
      setRows((current) => current.map((row) => accountEnvironmentRowKey(row) === accountEnvironmentRowKey(updated) ? updated : row));
      setEditing(null);
      setDraft(null);
    } catch (saveError) {
      setError(errorText(saveError));
    } finally {
      setSaving(false);
    }
  }

  async function refreshObservations() {
    setLoading(true);
    setError('');
    try {
      setRows(await api<AccountEnvironmentBinding[]>('/account-environment-bindings/refresh-observations', { method: 'POST' }));
    } catch (refreshError) {
      setError(errorText(refreshError));
    } finally {
      setLoading(false);
    }
  }

  function environmentControls(placeholder: string, showObservationRefresh = false) {
    return (
      <Space style={{ marginBottom: 12 }}>
        <Input.Search
          allowClear
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          onSearch={(value) => void loadEnvironment(value)}
          placeholder={placeholder}
          style={{ width: 320 }}
        />
        <Button onClick={() => void loadEnvironment()} loading={loading}>刷新</Button>
        {showObservationRefresh && <Button disabled={!canManageEnvironment} onClick={() => void refreshObservations()} loading={loading}>刷新远端观测</Button>}
      </Space>
    );
  }

  const proxyTable = (
    <>
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
      {environmentControls('账号、应用、代理')}
      <BatchProxyBindingPanel canManageEnvironment={canManageEnvironment} onBound={() => void loadEnvironment()} />
      <Table
        rowKey={(row) => accountEnvironmentRowKey(row)}
        size="small"
        loading={loading}
        dataSource={rows}
        columns={[
          { title: '账号', key: 'account', render: (_, row) => `${row.account_display_name}${row.account_username ? ` @${row.account_username}` : ''}` },
          { title: '应用', key: 'app', render: (_, row) => row.developer_app_name || `App #${row.developer_app_id || '-'}` },
          { title: 'api_id', dataIndex: 'developer_app_api_id_snapshot' },
          { title: '授权ID', dataIndex: 'authorization_id' },
          { title: '授权槽位', dataIndex: 'session_role' },
          { title: '代理', key: 'proxy', render: (_, row) => row.proxy_name || (row.proxy_id ? `Proxy #${row.proxy_id}` : '-') },
          { title: '代理状态', dataIndex: 'proxy_status' },
          { title: '生效边界', dataIndex: 'effect_boundary' },
          { title: '操作', key: 'action', render: (_, row) => <Button size="small" disabled={!canManageEnvironment} onClick={() => openEdit(row)}>编辑代理</Button> },
        ]}
      />
    </>
  );

  const fingerprintTable = (
    <>
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
      {environmentControls('账号、应用、指纹', true)}
      <Table
        rowKey={(row) => accountEnvironmentRowKey(row)}
        size="small"
        loading={loading}
        dataSource={rows}
        columns={[
          { title: '账号', key: 'account', render: (_, row) => `${row.account_display_name}${row.account_username ? ` @${row.account_username}` : ''}` },
          { title: '应用', key: 'app', render: (_, row) => row.developer_app_name || `App #${row.developer_app_id || '-'}` },
          { title: 'api_id', dataIndex: 'developer_app_api_id_snapshot' },
          { title: '授权ID', dataIndex: 'authorization_id' },
          { title: '授权槽位', dataIndex: 'session_role' },
          { title: '配置指纹', key: 'device', render: (_, row) => [row.device_model, row.system_version, row.app_version].filter(Boolean).join(' / ') || '-' },
          { title: '远端观测', key: 'observed', render: (_, row) => observedFingerprintText(row) },
          { title: '状态', key: 'status', render: (_, row) => statusTag(row.consistency_status) },
          { title: '操作', key: 'action', render: (_, row) => <Button size="small" disabled={!canManageEnvironment} onClick={() => openEdit(row)}>编辑指纹</Button> },
        ]}
      />
    </>
  );

  return (
    <>
      <Tabs
        className="account-mask-tabs"
        items={[
          {
            key: 'profiles',
            label: '面具管理',
            children: <AIAccountVoiceProfilesView canManageVoiceProfiles={canManageVoiceProfiles} />,
          },
          { key: 'proxies', label: '账号代理', children: proxyTable },
          { key: 'fingerprints', label: '授权指纹', children: fingerprintTable },
          {
            key: 'audit',
            label: '异常与审计',
            children: <Table rowKey={(row) => accountEnvironmentRowKey(row)} size="small" dataSource={rows.filter((row) => row.consistency_status !== 'observed_matched')} columns={[
              { title: '账号', dataIndex: 'account_display_name' },
              { title: '应用', dataIndex: 'developer_app_name' },
              { title: '授权ID', dataIndex: 'authorization_id' },
              { title: '授权槽位', dataIndex: 'session_role' },
              { title: '配置指纹', key: 'configured', render: (_, row) => [row.device_model, row.system_version, row.app_version].filter(Boolean).join(' / ') || '-' },
              { title: '远端观测', key: 'observed', render: (_, row) => [row.observed_device_model, row.observed_system_version, row.observed_app_version].filter(Boolean).join(' / ') || '-' },
              { title: '状态', key: 'status', render: (_, row) => statusTag(row.consistency_status) },
              { title: '边界', dataIndex: 'effect_boundary' },
            ]} />,
          },
        ]}
      />
      <Modal
        title="授权环境"
        open={Boolean(editing && draft)}
        onCancel={() => setEditing(null)}
        onOk={saveEnvironment}
        confirmLoading={saving}
        okButtonProps={{ disabled: !canManageEnvironment }}
        destroyOnHidden
      >
        {editing && <Descriptions size="small" column={1} items={[
          { key: 'account', label: '账号', children: editing.account_display_name },
          { key: 'app', label: '应用', children: editing.developer_app_name || `App #${editing.developer_app_id || '-'}` },
          { key: 'authorization', label: '授权ID', children: editing.authorization_id || '-' },
          { key: 'role', label: '授权槽位', children: editing.session_role },
          { key: 'effect', label: '生效边界', children: editing.effect_boundary },
        ]} />}
        {draft && <Form layout="vertical">
          <Form.Item label="代理 ID"><InputNumber value={draft.proxy_id || undefined} onChange={(value) => updateDraft('proxy_id', value)} style={{ width: '100%' }} /></Form.Item>
          <Form.Item label="设备型号"><Input value={draft.device_model} onChange={(event) => updateDraft('device_model', event.target.value)} /></Form.Item>
          <Form.Item label="系统版本"><Input value={draft.system_version} onChange={(event) => updateDraft('system_version', event.target.value)} /></Form.Item>
          <Form.Item label="App 版本"><Input value={draft.app_version} onChange={(event) => updateDraft('app_version', event.target.value)} /></Form.Item>
          <Form.Item label="平台"><Input value={draft.platform} onChange={(event) => updateDraft('platform', event.target.value)} /></Form.Item>
          <Form.Item label="语言"><Input value={draft.lang_code} onChange={(event) => updateDraft('lang_code', event.target.value)} /></Form.Item>
          <Form.Item label="系统语言"><Input value={draft.system_lang_code} onChange={(event) => updateDraft('system_lang_code', event.target.value)} /></Form.Item>
          <Form.Item label="地区"><Input value={draft.region_code} onChange={(event) => updateDraft('region_code', event.target.value)} /></Form.Item>
          <Form.Item label="身份键"><Input value={draft.client_identity_key} onChange={(event) => updateDraft('client_identity_key', event.target.value)} /></Form.Item>
        </Form>}
      </Modal>
    </>
  );
}
