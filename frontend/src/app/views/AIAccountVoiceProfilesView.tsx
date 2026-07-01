import React from 'react';
import { Alert, Button, Card, Empty, Input, Modal, Select, Space, Table, Tag, Typography } from 'antd';
import { api } from '../../shared/api/client';
import type {
  AiAccountVoiceProfile,
  AiAccountVoiceProfileAudit,
  AiAccountVoiceProfileBatchRebuildOut,
  AiAccountVoiceProfileBatchResultItem,
  AiAccountVoiceProfileBatchStatusOut,
  AiAccountVoiceProfileVersion,
} from '../types';

const PROFILE_STATUS_OPTIONS = [
  { value: '', label: '全部面具' },
  { value: 'missing', label: '缺面具' },
  { value: 'active', label: '已启用' },
  { value: 'disabled', label: '已停用' },
];

const TEXT_LIST_FIELDS = [
  'persona_experiences',
  'consumption_experiences',
  'interaction_habits',
  'lexical_preferences',
  'forbidden_expressions',
  'preference_tags',
] as const;

type TextListField = typeof TEXT_LIST_FIELDS[number];

type EditableProfile = Pick<
  AiAccountVoiceProfile,
  | 'mask_name'
  | 'audience_archetype'
  | 'identity_frame'
  | 'age_band'
  | 'sentence_length'
  | 'tone_strength'
  | 'emoji_policy'
  | 'short_prompt_summary'
  | 'quality_status'
  | 'profile_status'
> & Pick<AiAccountVoiceProfile, TextListField>;

interface Props {
  canManageVoiceProfiles?: boolean;
}

function errorText(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}

function accountTitle(profile: AiAccountVoiceProfile) {
  const username = profile.username ? ` @${profile.username}` : '';
  return `${profile.display_name || `账号 #${profile.account_id}`}${username}`;
}

function splitLines(value: string) {
  return value.split('\n').map((line) => line.trim()).filter(Boolean);
}

function joinLines(value: string[]) {
  return value.join('\n');
}

function editableFromProfile(profile: AiAccountVoiceProfile): EditableProfile {
  return {
    age_band: profile.age_band,
    mask_name: profile.mask_name,
    audience_archetype: profile.audience_archetype,
    identity_frame: profile.identity_frame,
    sentence_length: profile.sentence_length,
    tone_strength: profile.tone_strength,
    emoji_policy: profile.emoji_policy,
    short_prompt_summary: profile.short_prompt_summary,
    quality_status: profile.quality_status,
    profile_status: profile.profile_status,
    persona_experiences: profile.persona_experiences,
    consumption_experiences: profile.consumption_experiences,
    interaction_habits: profile.interaction_habits,
    lexical_preferences: profile.lexical_preferences,
    forbidden_expressions: profile.forbidden_expressions,
    preference_tags: profile.preference_tags,
  };
}

function payloadFromDraft(draft: EditableProfile) {
  return {
    age_band: draft.age_band.trim(),
    mask_name: draft.mask_name.trim(),
    audience_archetype: draft.audience_archetype.trim(),
    identity_frame: draft.identity_frame.trim(),
    sentence_length: draft.sentence_length.trim(),
    tone_strength: draft.tone_strength.trim(),
    emoji_policy: draft.emoji_policy.trim(),
    short_prompt_summary: draft.short_prompt_summary.trim(),
    quality_status: draft.quality_status.trim(),
    status: draft.profile_status,
    persona_experiences: draft.persona_experiences,
    consumption_experiences: draft.consumption_experiences,
    interaction_habits: draft.interaction_habits,
    lexical_preferences: draft.lexical_preferences,
    forbidden_expressions: draft.forbidden_expressions,
    preference_tags: draft.preference_tags,
  };
}

function profileStatusTag(status: string) {
  if (status === 'missing') return <Tag color="red">缺面具</Tag>;
  if (status === 'active') return <Tag color="green">已启用</Tag>;
  if (status === 'disabled') return <Tag color="default">已停用</Tag>;
  return <Tag>{status || '-'}</Tag>;
}

function batchResultStatusTag(status: string) {
  if (status === 'created') return <Tag color="green">已生成</Tag>;
  if (status === 'failed') return <Tag color="red">失败</Tag>;
  if (status === 'skipped') return <Tag color="default">跳过</Tag>;
  return <Tag>{status || '-'}</Tag>;
}

function batchResultNotice(prefix: string, result: AiAccountVoiceProfileBatchRebuildOut) {
  const failed = result.items.filter((item) => item.status === 'failed').length;
  return `${prefix}完成：新增 ${result.created}，跳过 ${result.skipped}，失败 ${failed}`;
}

export default function AIAccountVoiceProfilesView({ canManageVoiceProfiles = false }: Props) {
  const [profiles, setProfiles] = React.useState<AiAccountVoiceProfile[]>([]);
  const [search, setSearch] = React.useState('');
  const [profileStatus, setProfileStatus] = React.useState('');
  const [loading, setLoading] = React.useState(false);
  const [savingKey, setSavingKey] = React.useState('');
  const [error, setError] = React.useState('');
  const [notice, setNotice] = React.useState('');
  const [editing, setEditing] = React.useState<AiAccountVoiceProfile | null>(null);
  const [historyProfile, setHistoryProfile] = React.useState<AiAccountVoiceProfile | null>(null);
  const [versions, setVersions] = React.useState<AiAccountVoiceProfileVersion[]>([]);
  const [audits, setAudits] = React.useState<AiAccountVoiceProfileAudit[]>([]);
  const [historyLoading, setHistoryLoading] = React.useState(false);
  const [draft, setDraft] = React.useState<EditableProfile | null>(null);
  const [selectedAccountIds, setSelectedAccountIds] = React.useState<number[]>([]);
  const [batchResultItems, setBatchResultItems] = React.useState<AiAccountVoiceProfileBatchResultItem[]>([]);
  const loadSeqRef = React.useRef(0);

  React.useEffect(() => {
    void loadProfiles();
  }, [profileStatus]);

  async function loadProfiles(nextSearch = search) {
    const requestSeq = loadSeqRef.current + 1;
    loadSeqRef.current = requestSeq;
    const params = new URLSearchParams();
    if (nextSearch.trim()) params.set('search', nextSearch.trim());
    if (profileStatus) params.set('profile_status', profileStatus);
    setLoading(true);
    setError('');
    try {
      const nextProfiles = await api<AiAccountVoiceProfile[]>('/ai-account-voice-profiles?' + params.toString());
      if (loadSeqRef.current === requestSeq) setProfiles(nextProfiles);
    } catch (loadError) {
      if (loadSeqRef.current === requestSeq) setError(errorText(loadError));
    } finally {
      if (loadSeqRef.current === requestSeq) setLoading(false);
    }
  }

  function openEdit(profile: AiAccountVoiceProfile) {
    setEditing(profile);
    setDraft(editableFromProfile(profile));
    setError('');
  }

  async function openHistory(profile: AiAccountVoiceProfile) {
    setHistoryProfile(profile);
    setHistoryLoading(true);
    setError('');
    try {
      const [nextVersions, nextAudits] = await Promise.all([
        api<AiAccountVoiceProfileVersion[]>(`/ai-account-voice-profiles/${profile.account_id}/versions`),
        api<AiAccountVoiceProfileAudit[]>(`/ai-account-voice-profiles/${profile.account_id}/audits`),
      ]);
      setVersions(nextVersions);
      setAudits(nextAudits);
    } catch (historyError) {
      setError(errorText(historyError));
    } finally {
      setHistoryLoading(false);
    }
  }

  function updateDraft(field: keyof EditableProfile, value: string | string[]) {
    setDraft((current) => current ? { ...current, [field]: value } : current);
  }

  async function saveProfile() {
    if (!editing || !draft) return;
    const profile = editing;
    const actionKey = `save:${profile.account_id}`;
    setSavingKey(actionKey);
    setError('');
    try {
      const updated = await api<AiAccountVoiceProfile>(`/ai-account-voice-profiles/${profile.account_id}`, {
        method: 'PATCH',
        body: JSON.stringify(payloadFromDraft(draft)),
      });
      setProfiles((current) => current.map((item) => item.account_id === updated.account_id ? updated : item));
      setEditing(null);
      setDraft(null);
      setNotice('账号面具已保存，下一轮规划生效');
    } catch (saveError) {
      setError(errorText(saveError));
    } finally {
      setSavingKey('');
    }
  }

  async function rebuildProfile(profile: AiAccountVoiceProfile) {
    const actionKey = `rebuild:${profile.account_id}`;
    setSavingKey(actionKey);
    setError('');
    try {
      const updated = await api<AiAccountVoiceProfile>(`/ai-account-voice-profiles/${profile.account_id}/rebuild`, { method: 'POST' });
      setProfiles((current) => current.map((item) => item.account_id === updated.account_id ? updated : item));
      setNotice(`${accountTitle(updated)} 已重建面具`);
    } catch (rebuildError) {
      setError(errorText(rebuildError));
    } finally {
      setSavingKey('');
    }
  }

  async function rollbackProfile(profile: AiAccountVoiceProfile, sourceVersion: number) {
    const actionKey = `rollback:${profile.account_id}:${sourceVersion}`;
    setSavingKey(actionKey);
    setError('');
    try {
      const updated = await api<AiAccountVoiceProfile>(`/ai-account-voice-profiles/${profile.account_id}/rollback`, {
        method: 'POST',
        body: JSON.stringify({ source_version: sourceVersion }),
      });
      setProfiles((current) => current.map((item) => item.account_id === updated.account_id ? updated : item));
      setNotice(`${accountTitle(updated)} 已回滚到 v${sourceVersion}，下一轮规划生效`);
      await openHistory(updated);
    } catch (rollbackError) {
      setError(errorText(rollbackError));
    } finally {
      setSavingKey('');
    }
  }

  async function batchRebuildMissing() {
    setSavingKey('batch-rebuild-missing');
    setError('');
    try {
      const result = await api<AiAccountVoiceProfileBatchRebuildOut>('/ai-account-voice-profiles/batch-rebuild', {
        method: 'POST',
        body: JSON.stringify({ account_ids: [], missing_only: true }),
        timeoutMs: 60_000,
      });
      setBatchResultItems(result.items || []);
      setNotice(batchResultNotice('批量补齐', result));
      await loadProfiles();
    } catch (batchError) {
      setError(errorText(batchError));
    } finally {
      setSavingKey('');
    }
  }

  async function batchRebuildSelected() {
    setSavingKey('batch-rebuild-selected');
    setError('');
    try {
      const result = await api<AiAccountVoiceProfileBatchRebuildOut>('/ai-account-voice-profiles/batch-rebuild', {
        method: 'POST',
        body: JSON.stringify({ account_ids: selectedAccountIds, missing_only: false }),
        timeoutMs: 60_000,
      });
      setBatchResultItems(result.items || []);
      setNotice(batchResultNotice('批量重建', result));
      setSelectedAccountIds([]);
      await loadProfiles();
    } catch (batchError) {
      setError(errorText(batchError));
    } finally {
      setSavingKey('');
    }
  }

  async function batchUpdateStatus(status: 'active' | 'disabled') {
    const actionKey = `batch-status:${status}`;
    setSavingKey(actionKey);
    setError('');
    try {
      const result = await api<AiAccountVoiceProfileBatchStatusOut>('/ai-account-voice-profiles/batch-status', {
        method: 'POST',
        body: JSON.stringify({ account_ids: selectedAccountIds, status }),
      });
      setNotice(`批量${status === 'active' ? '恢复' : '停用'}完成：更新 ${result.updated}，跳过 ${result.skipped}`);
      setSelectedAccountIds([]);
      await loadProfiles();
    } catch (batchError) {
      setError(errorText(batchError));
    } finally {
      setSavingKey('');
    }
  }

  const rowSelection = canManageVoiceProfiles ? {
    selectedRowKeys: selectedAccountIds,
    preserveSelectedRowKeys: true,
    onChange: (keys: React.Key[]) => setSelectedAccountIds(keys.map((key) => Number(key))),
  } : undefined;

  const columns = [
    { title: '账号', render: (_: unknown, profile: AiAccountVoiceProfile) => <Space direction="vertical" size={0}><Typography.Text>{accountTitle(profile)}</Typography.Text><Typography.Text type="secondary">{profile.phone_masked || '-'}</Typography.Text></Space> },
    { title: '状态', render: (_: unknown, profile: AiAccountVoiceProfile) => <Space>{profileStatusTag(profile.profile_status)}<Tag>{profile.account_status || '-'}</Tag></Space> },
    { title: '版本', dataIndex: 'version', width: 70 },
    { title: '面具', dataIndex: 'mask_name', ellipsis: true, render: (value: string) => value || '-' },
    { title: '摘要', dataIndex: 'short_prompt_summary', ellipsis: true },
    { title: '差异度', dataIndex: 'similarity_score', width: 90, render: (value: number | null) => value ?? '-' },
    { title: '更新', width: 170, render: (_: unknown, profile: AiAccountVoiceProfile) => profile.updated_at ? profile.updated_at.replace('T', ' ').slice(0, 16) : '-' },
    { title: '操作', width: 250, render: (_: unknown, profile: AiAccountVoiceProfile) => <Space><Button size="small" onClick={() => openEdit(profile)}>编辑</Button><Button size="small" onClick={() => openHistory(profile)}>版本历史</Button><Button size="small" disabled={!canManageVoiceProfiles} loading={savingKey === `rebuild:${profile.account_id}`} onClick={() => rebuildProfile(profile)}>重建</Button></Space> },
  ];

  const versionColumns = [
    { title: '版本', dataIndex: 'version', width: 70 },
    { title: '状态', dataIndex: 'status', width: 90, render: (status: string) => profileStatusTag(status) },
    { title: '来源', dataIndex: 'source', width: 90 },
    { title: '摘要', dataIndex: 'short_prompt_summary', ellipsis: true },
    { title: '更新人', dataIndex: 'updated_by', width: 120 },
    { title: '更新时间', width: 160, render: (_: unknown, row: AiAccountVoiceProfileVersion) => row.updated_at ? row.updated_at.replace('T', ' ').slice(0, 16) : '-' },
    {
      title: '操作',
      width: 130,
      render: (_: unknown, row: AiAccountVoiceProfileVersion) => (
        <Button
          size="small"
          disabled={!canManageVoiceProfiles || row.status === 'active' || !historyProfile}
          loading={savingKey === `rollback:${historyProfile?.account_id}:${row.version}`}
          onClick={() => historyProfile && rollbackProfile(historyProfile, row.version)}
        >
          回滚到此版本
        </Button>
      ),
    },
  ];

  const auditColumns = [
    { title: '时间', width: 160, render: (_: unknown, row: AiAccountVoiceProfileAudit) => row.created_at ? row.created_at.replace('T', ' ').slice(0, 16) : '-' },
    { title: '操作人', dataIndex: 'actor', width: 120 },
    { title: '动作', dataIndex: 'action', width: 140 },
    { title: '详情', dataIndex: 'detail', ellipsis: true },
  ];

  const batchResultColumns = [
    { title: '账号', dataIndex: 'account_id', width: 100, render: (value: number) => `#${value}` },
    { title: '结果', dataIndex: 'status', width: 90, render: (status: string) => batchResultStatusTag(status) },
    { title: '版本', dataIndex: 'version', width: 70 },
    { title: '差异度', dataIndex: 'similarity_score', width: 90, render: (value: number | null) => value ?? '-' },
    { title: '失败原因', dataIndex: 'failure_reason', ellipsis: true, render: (value: string) => value || '-' },
    { title: '跳过原因', dataIndex: 'skipped_reason', ellipsis: true, render: (value: string) => value || '-' },
  ];

  return (
    <Card className="panel" title="账号面具" extra={<Typography.Text type="secondary">账号级全局真人感原则</Typography.Text>}>
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        {!canManageVoiceProfiles && <Alert type="warning" showIcon message="当前账号没有账号面具管理权限，只能查看。" />}
        {error && <Alert type="error" showIcon message={error} />}
        {notice && <Alert type="success" showIcon message={notice} closable onClose={() => setNotice('')} />}
        <Space wrap>
          <Input.Search
            allowClear
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            onSearch={(value) => { setSearch(value); void loadProfiles(value); }}
            placeholder="搜索账号名 / username / 手机号后四位 / 面具名称"
            style={{ width: 320 }}
          />
          <Select value={profileStatus} onChange={setProfileStatus} options={PROFILE_STATUS_OPTIONS} style={{ width: 160 }} />
          <Button onClick={() => loadProfiles()} loading={loading}>刷新</Button>
          <Button type="primary" disabled={!canManageVoiceProfiles} loading={savingKey === 'batch-rebuild-missing'} onClick={batchRebuildMissing}>批量补齐缺面具账号</Button>
          <Button disabled={!canManageVoiceProfiles || !selectedAccountIds.length} loading={savingKey === 'batch-rebuild-selected'} onClick={batchRebuildSelected}>批量重建</Button>
          <Button disabled={!canManageVoiceProfiles || !selectedAccountIds.length} loading={savingKey === 'batch-status:disabled'} onClick={() => batchUpdateStatus('disabled')}>批量停用</Button>
          <Button disabled={!canManageVoiceProfiles || !selectedAccountIds.length} loading={savingKey === 'batch-status:active'} onClick={() => batchUpdateStatus('active')}>批量恢复</Button>
        </Space>
        <Table
          rowKey="account_id"
          size="small"
          loading={loading}
          dataSource={profiles}
          columns={columns}
          rowSelection={rowSelection}
          locale={{ emptyText: <Empty description="暂无账号面具数据" /> }}
          pagination={{ pageSize: 20, showSizeChanger: false }}
        />
        {batchResultItems.length > 0 && (
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <Typography.Title level={5} style={{ margin: 0 }}>批量生成结果</Typography.Title>
            <Table
              rowKey={(item) => `${item.account_id}:${item.status}:${item.version}`}
              size="small"
              dataSource={batchResultItems}
              columns={batchResultColumns}
              pagination={false}
              scroll={{ x: 760 }}
            />
          </Space>
        )}
      </Space>
      <Modal
        className="tg-modal large"
        title={editing ? `编辑面具：${accountTitle(editing)}` : '编辑面具'}
        open={Boolean(editing && draft)}
        width={860}
        okText="保存"
        cancelText="取消"
        confirmLoading={savingKey.startsWith('save:')}
        okButtonProps={{ disabled: !canManageVoiceProfiles }}
        onOk={saveProfile}
        onCancel={() => { setEditing(null); setDraft(null); }}
        destroyOnHidden
        centered
      >
        {draft && (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <div className="policy-grid">
              <label>面具名称<Input value={draft.mask_name} onChange={(event) => updateDraft('mask_name', event.target.value)} /></label>
              <label>人群设定<Input value={draft.audience_archetype} onChange={(event) => updateDraft('audience_archetype', event.target.value)} /></label>
              <label>身份框架<Input value={draft.identity_frame} onChange={(event) => updateDraft('identity_frame', event.target.value)} /></label>
              <label>年龄段<Input value={draft.age_band} onChange={(event) => updateDraft('age_band', event.target.value)} /></label>
              <label>句长偏好<Input value={draft.sentence_length} onChange={(event) => updateDraft('sentence_length', event.target.value)} /></label>
              <label>语气强度<Input value={draft.tone_strength} onChange={(event) => updateDraft('tone_strength', event.target.value)} /></label>
              <label>表情策略<Input value={draft.emoji_policy} onChange={(event) => updateDraft('emoji_policy', event.target.value)} /></label>
              <label>质量状态<Input value={draft.quality_status} onChange={(event) => updateDraft('quality_status', event.target.value)} /></label>
              <label>面具状态<Select value={draft.profile_status} onChange={(value) => updateDraft('profile_status', value)} options={PROFILE_STATUS_OPTIONS.filter((option) => option.value)} /></label>
            </div>
            <label>短摘要<Input.TextArea rows={2} value={draft.short_prompt_summary} onChange={(event) => updateDraft('short_prompt_summary', event.target.value)} /></label>
            <label>偏好标签<Input.TextArea rows={3} value={joinLines(draft.preference_tags)} onChange={(event) => updateDraft('preference_tags', splitLines(event.target.value))} /></label>
            <label>经历设定<Input.TextArea rows={3} value={joinLines(draft.persona_experiences)} onChange={(event) => updateDraft('persona_experiences', splitLines(event.target.value))} /></label>
            <label>消费经历设定<Input.TextArea rows={3} value={joinLines(draft.consumption_experiences)} onChange={(event) => updateDraft('consumption_experiences', splitLines(event.target.value))} /></label>
            <label>互动习惯<Input.TextArea rows={3} value={joinLines(draft.interaction_habits)} onChange={(event) => updateDraft('interaction_habits', splitLines(event.target.value))} /></label>
            <label>用词偏好<Input.TextArea rows={3} value={joinLines(draft.lexical_preferences)} onChange={(event) => updateDraft('lexical_preferences', splitLines(event.target.value))} /></label>
            <label>禁用表达<Input.TextArea rows={3} value={joinLines(draft.forbidden_expressions)} onChange={(event) => updateDraft('forbidden_expressions', splitLines(event.target.value))} /></label>
          </Space>
        )}
      </Modal>
      <Modal
        className="tg-modal large"
        title={historyProfile ? `版本历史：${accountTitle(historyProfile)}` : '版本历史'}
        open={Boolean(historyProfile)}
        width={920}
        footer={null}
        onCancel={() => { setHistoryProfile(null); setVersions([]); setAudits([]); }}
        destroyOnHidden
        centered
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Typography.Text type="secondary">账号面具回滚会创建一个新的启用版本，已生成的 action 仍使用旧版本。</Typography.Text>
          <Table
            rowKey="version"
            size="small"
            loading={historyLoading}
            dataSource={versions}
            columns={versionColumns}
            pagination={false}
            scroll={{ x: 880 }}
          />
          <Typography.Title level={5} style={{ margin: 0 }}>审计记录</Typography.Title>
          <Table
            rowKey="id"
            size="small"
            loading={historyLoading}
            dataSource={audits}
            columns={auditColumns}
            pagination={false}
            scroll={{ x: 760 }}
          />
        </Space>
      </Modal>
    </Card>
  );
}
