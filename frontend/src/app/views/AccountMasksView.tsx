import React from 'react';
import { Alert, Button, Descriptions, Form, Input, InputNumber, Modal, Space, Table, Tabs, Tag } from 'antd';
import { api } from '../../shared/api/client';
import type { AccountEnvironmentBinding, CurrentUser } from '../types';
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

function errorText(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}

function statusTag(status: string) {
  if (status === 'pending_effect') return <Tag color="gold">待下次生效</Tag>;
  if (status === 'observed_matched') return <Tag color="green">已匹配</Tag>;
  if (status === 'observed_mismatch') return <Tag color="red">不一致</Tag>;
  return <Tag>{status || '未连接'}</Tag>;
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
      setRows((current) => current.map((row) => row.authorization_id === updated.authorization_id ? updated : row));
      setEditing(null);
      setDraft(null);
    } catch (saveError) {
      setError(errorText(saveError));
    } finally {
      setSaving(false);
    }
  }

  const environmentTable = (
    <>
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
      <Space style={{ marginBottom: 12 }}>
        <Input.Search
          allowClear
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          onSearch={(value) => void loadEnvironment(value)}
          placeholder="账号、应用、代理、指纹"
          style={{ width: 320 }}
        />
        <Button onClick={() => void loadEnvironment()} loading={loading}>刷新</Button>
      </Space>
      <Table
        rowKey={(row) => `${row.account_id}:${row.authorization_id}:${row.session_role}`}
        size="small"
        loading={loading}
        dataSource={rows}
        columns={[
          { title: '账号', key: 'account', render: (_, row) => `${row.account_display_name}${row.account_username ? ` @${row.account_username}` : ''}` },
          { title: '应用', key: 'app', render: (_, row) => row.developer_app_name || `App #${row.developer_app_id || '-'}` },
          { title: 'api_id', dataIndex: 'developer_app_api_id_snapshot' },
          { title: '授权槽位', dataIndex: 'session_role' },
          { title: '代理', key: 'proxy', render: (_, row) => row.proxy_name || (row.proxy_id ? `Proxy #${row.proxy_id}` : '-') },
          { title: '配置指纹', key: 'device', render: (_, row) => [row.device_model, row.system_version, row.app_version].filter(Boolean).join(' / ') || '-' },
          { title: '远端观测', key: 'observed', render: (_, row) => [row.observed_device_model, row.observed_system_version, row.observed_app_version].filter(Boolean).join(' / ') || '-' },
          { title: '状态', key: 'status', render: (_, row) => statusTag(row.consistency_status) },
          { title: '操作', key: 'action', render: (_, row) => <Button size="small" disabled={!canManageEnvironment} onClick={() => openEdit(row)}>编辑</Button> },
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
          { key: 'proxies', label: '账号代理', children: environmentTable },
          { key: 'fingerprints', label: '授权指纹', children: environmentTable },
          {
            key: 'audit',
            label: '异常与审计',
            children: <Table rowKey={(row) => `${row.account_id}:${row.authorization_id}`} size="small" dataSource={rows.filter((row) => row.consistency_status !== 'observed_matched')} columns={[
              { title: '账号', dataIndex: 'account_display_name' },
              { title: '应用', dataIndex: 'developer_app_name' },
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
