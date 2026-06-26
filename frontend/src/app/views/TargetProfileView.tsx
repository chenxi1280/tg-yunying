import React from 'react';
import { Alert, App as AntdApp, Button, Card, Descriptions, Empty, Form, Input, InputNumber, Select, Space, Switch, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { RefreshCcw, ShieldAlert } from 'lucide-react';
import { api } from '../../shared/api/client';
import { StatusBadge } from '../components/shared';
import { candidateKey, errorMessage, formToRule, formatDateTime, ruleToForm, selectedSourceKeys, TASK_LABELS } from './targetProfileViewModel';
import type { LearningRun, LearningSample, LearningSource, ProfileVersion, QualityRule, QualityRuleForm, SourceCandidate, TargetProfileOverview, TargetProfileUsage } from './targetProfileViewModel';

type Props = {
  canManage: boolean;
  onOpenTargets: () => void;
};

type ProfileActionRequestOptions = {
  actionKey: string;
  payloadSignature: string;
  currentPayloadSignature: () => string;
};

export default function TargetProfileView({ canManage, onOpenTargets }: Props) {
  const { message } = AntdApp.useApp();
  const [loading, setLoading] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState('');
  const [profile, setProfile] = React.useState<TargetProfileOverview | null>(null);
  const [usage, setUsage] = React.useState<TargetProfileUsage | null>(null);
  const [candidates, setCandidates] = React.useState<SourceCandidate[]>([]);
  const [sources, setSources] = React.useState<LearningSource[]>([]);
  const [samples, setSamples] = React.useState<LearningSample[]>([]);
  const [runs, setRuns] = React.useState<LearningRun[]>([]);
  const [versions, setVersions] = React.useState<ProfileVersion[]>([]);
  const [selectedSourceIds, setSelectedSourceIds] = React.useState<React.Key[]>([]);
  const [qualityRule, setQualityRule] = React.useState<QualityRule | null>(null);
  const [form] = Form.useForm<QualityRuleForm>();
  const activeProfileDataRequestSeq = React.useRef(0);
  const activeProfileActionRequestRef = React.useRef({ seq: 0, actionKey: '', signature: '' });

  function beginProfileDataRequest() {
    activeProfileDataRequestSeq.current += 1;
    return activeProfileDataRequestSeq.current;
  }

  function isActiveProfileDataRequest(requestSeq: number) {
    return activeProfileDataRequestSeq.current === requestSeq;
  }

  function targetProfileActionPayloadSignature(actionKey: string, payload: Record<string, unknown>) {
    return JSON.stringify({ action_key: actionKey, payload });
  }

  function beginProfileActionRequest(actionKey: string, signature: string) {
    activeProfileDataRequestSeq.current += 1;
    const requestSeq = activeProfileActionRequestRef.current.seq + 1;
    activeProfileActionRequestRef.current = { seq: requestSeq, actionKey, signature };
    return requestSeq;
  }

  function isCurrentProfileActionRequest(requestSeq: number) {
    return activeProfileActionRequestRef.current.seq === requestSeq;
  }

  function isActiveProfileActionRequest(
    requestSeq: number,
    actionKey: string,
    signature: string,
    currentSignature: () => string,
  ) {
    return isCurrentProfileActionRequest(requestSeq)
      && activeProfileActionRequestRef.current.actionKey === actionKey
      && activeProfileActionRequestRef.current.signature === signature
      && currentSignature() === signature;
  }

  async function fetchTargetProfileData(requestSeq: number) {
    const [profileResult, usageResult, candidateResult, sourceResult, sampleResult, runResult, versionResult, ruleResult] = await Promise.all([
      api<TargetProfileOverview>('/target-profile'),
      api<TargetProfileUsage>('/target-profile/usage'),
      api<{ items: SourceCandidate[] }>('/target-profile/source-candidates'),
      api<{ items: LearningSource[] }>('/target-profile/sources'),
      api<{ items: LearningSample[] }>('/target-profile/samples'),
      api<{ items: LearningRun[] }>('/target-profile/runs'),
      api<{ items: ProfileVersion[] }>('/target-profile/versions'),
      api<QualityRule>('/target-profile/quality-rules'),
    ]);
    if (!isActiveProfileDataRequest(requestSeq)) return false;
    setProfile(profileResult);
    setUsage(usageResult);
    setCandidates(candidateResult.items || []);
    setSources(sourceResult.items || []);
    setSamples(sampleResult.items || []);
    setRuns(runResult.items || []);
    setVersions(versionResult.items || []);
    setQualityRule(ruleResult);
    setSelectedSourceIds(selectedSourceKeys(sourceResult.items || [], candidateResult.items || []));
    form.setFieldsValue(ruleToForm(ruleResult));
    return true;
  }

  async function load() {
    const requestSeq = beginProfileDataRequest();
    setLoading(true);
    setError('');
    try {
      await fetchTargetProfileData(requestSeq);
    } catch (err) {
      if (!isActiveProfileDataRequest(requestSeq)) return;
      setError(errorMessage(err));
    } finally {
      if (isActiveProfileDataRequest(requestSeq)) setLoading(false);
    }
  }

  async function refreshTargetProfileAfterAction(actionLabel: string) {
    const requestSeq = beginProfileDataRequest();
    try {
      await fetchTargetProfileData(requestSeq);
    } catch (err) {
      if (!isActiveProfileDataRequest(requestSeq)) return;
      setError(`目标画像数据刷新失败：${actionLabel}操作已完成，但刷新目标画像数据失败：${errorMessage(err)}`);
    }
  }

  React.useEffect(() => {
    void load();
  }, []);

  function requireReason(prompt: string) {
    const reason = window.prompt(prompt);
    return reason?.trim() || '';
  }

  function confirmDangerAction(prompt: string) {
    return window.confirm(prompt);
  }

  function selectedSourcesPayload(reason: string) {
    const selectedKeySet = new Set(selectedSourceIds.map((item) => String(item)));
    const selected = candidates.filter((item) => selectedKeySet.has(candidateKey(item)));
    return {
      reason,
      sources: selected.map((item) => ({
        group_id: item.group_id,
        target_id: item.target_id,
        is_enabled: true,
        auto_sync_enabled: true,
        listener_account_ids: item.listener_account_ids,
      })),
    };
  }

  async function runAction(options: ProfileActionRequestOptions, action: () => Promise<void>, successText: string) {
    const requestSeq = beginProfileActionRequest(options.actionKey, options.payloadSignature);
    setSaving(true);
    setError('');
    try {
      await action();
      if (!isActiveProfileActionRequest(
        requestSeq,
        options.actionKey,
        options.payloadSignature,
        options.currentPayloadSignature,
      )) return;
      void message.success(successText);
      await refreshTargetProfileAfterAction(successText);
    } catch (err) {
      if (!isActiveProfileActionRequest(
        requestSeq,
        options.actionKey,
        options.payloadSignature,
        options.currentPayloadSignature,
      )) return;
      setError(errorMessage(err));
    } finally {
      if (isCurrentProfileActionRequest(requestSeq)) setSaving(false);
    }
  }

  async function saveSelectedSources() {
    const reason = requireReason('填写保存学习来源的原因');
    if (!reason) return;
    const actionKey = 'sources:save';
    const payload = selectedSourcesPayload(reason);
    const payloadSignature = targetProfileActionPayloadSignature(actionKey, payload);
    await runAction({
      actionKey,
      payloadSignature,
      currentPayloadSignature: () => targetProfileActionPayloadSignature(actionKey, selectedSourcesPayload(reason)),
    }, async () => {
      await api('/target-profile/sources', {
        method: 'PUT',
        body: JSON.stringify(payload),
      });
    }, `已保存 ${payload.sources.length} 个学习来源`);
  }

  async function sourceRun(source: LearningSource, path: 'sync' | 'pull-history') {
    const actionKey = `source:${path}`;
    const payload = { source_id: source.id, path };
    const payloadSignature = targetProfileActionPayloadSignature(actionKey, payload);
    await runAction({
      actionKey,
      payloadSignature,
      currentPayloadSignature: () => payloadSignature,
    }, async () => {
      await api(`/target-profile/sources/${source.id}/${path}`, { method: 'POST' });
    }, path === 'sync' ? '已同步来源' : '已开始上拉历史');
  }

  async function saveQualityRules() {
    const reason = requireReason('填写质量过滤规则变更原因');
    if (!reason) return;
    const values = await form.validateFields();
    const actionKey = 'quality-rules:save';
    const payload = formToRule(values, reason);
    const payloadSignature = targetProfileActionPayloadSignature(actionKey, payload);
    await runAction({
      actionKey,
      payloadSignature,
      currentPayloadSignature: () => targetProfileActionPayloadSignature(actionKey, formToRule(form.getFieldsValue(true), reason)),
    }, async () => {
      await api('/target-profile/quality-rules', {
        method: 'PATCH',
        body: JSON.stringify(payload),
      });
    }, '已保存样本质量规则');
  }

  async function updateSampleStatus(sample: LearningSample, learningStatus: string) {
    const reason = requireReason('填写样本状态调整原因');
    if (!reason) return;
    const actionKey = 'sample:status';
    const payload = { sample_id: sample.id, learning_status: learningStatus, reason };
    const payloadSignature = targetProfileActionPayloadSignature(actionKey, payload);
    await runAction({
      actionKey,
      payloadSignature,
      currentPayloadSignature: () => payloadSignature,
    }, async () => {
      await api(`/target-profile/samples/${sample.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ learning_status: learningStatus, reason }),
      });
    }, '已调整样本状态');
  }

  async function rebuildProfile() {
    if (!confirmDangerAction('确定重建全站目标画像？')) return;
    const reason = requireReason('填写重建全站目标画像的原因');
    if (!reason) return;
    const actionKey = 'profile:rebuild';
    const payload = { reason };
    const payloadSignature = targetProfileActionPayloadSignature(actionKey, payload);
    await runAction({
      actionKey,
      payloadSignature,
      currentPayloadSignature: () => payloadSignature,
    }, async () => {
      await api('/target-profile/rebuild', { method: 'POST', body: JSON.stringify(payload) });
    }, '已重建全站目标画像');
  }

  async function clearProfile() {
    if (!confirmDangerAction('确定清空全站目标画像？')) return;
    const reason = requireReason('填写清空全站目标画像的原因');
    if (!reason) return;
    const actionKey = 'profile:clear';
    const payload = { reason };
    const payloadSignature = targetProfileActionPayloadSignature(actionKey, payload);
    await runAction({
      actionKey,
      payloadSignature,
      currentPayloadSignature: () => payloadSignature,
    }, async () => {
      await api('/target-profile/clear', { method: 'POST', body: JSON.stringify(payload) });
    }, '已清空全站目标画像');
  }

  async function updateLearningEnabled(learningEnabled: boolean) {
    const reason = requireReason(learningEnabled ? '填写恢复画像学习的原因' : '填写暂停画像学习的原因');
    if (!reason) return;
    const actionKey = 'settings:learning-enabled';
    const payload = { learning_enabled: learningEnabled, reason };
    const payloadSignature = targetProfileActionPayloadSignature(actionKey, payload);
    await runAction({
      actionKey,
      payloadSignature,
      currentPayloadSignature: () => payloadSignature,
    }, async () => {
      await api('/target-profile/settings', {
        method: 'PATCH',
        body: JSON.stringify(payload),
      });
    }, learningEnabled ? '已恢复画像学习' : '已暂停画像学习');
  }

  async function restoreVersion(version: ProfileVersion) {
    const reason = requireReason(`填写恢复 v${version.profile_version} 的原因`);
    if (!reason) return;
    const actionKey = 'version:restore';
    const payload = { version_id: version.id, reason };
    const payloadSignature = targetProfileActionPayloadSignature(actionKey, payload);
    await runAction({
      actionKey,
      payloadSignature,
      currentPayloadSignature: () => payloadSignature,
    }, async () => {
      await api(`/target-profile/versions/${version.id}/restore`, {
        method: 'POST',
        body: JSON.stringify({ reason }),
      });
    }, `已恢复 v${version.profile_version}`);
  }

  async function recomputeCandidates() {
    const reason = requireReason('填写重算候选样本的原因');
    if (!reason) return;
    const actionKey = 'candidates:recompute';
    const payload = { reason };
    const payloadSignature = targetProfileActionPayloadSignature(actionKey, payload);
    await runAction({
      actionKey,
      payloadSignature,
      currentPayloadSignature: () => payloadSignature,
    }, async () => {
      await api('/target-profile/recompute-candidates', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
    }, '已重算候选样本');
  }

  const sourceColumns: ColumnsType<LearningSource> = [
    { title: '来源', key: 'target', render: (_, item) => <Space direction="vertical" size={0}><Typography.Text strong>{item.target_title}</Typography.Text><Typography.Text type="secondary">{item.target_type} / #{item.target_id}</Typography.Text></Space> },
    { title: '状态', key: 'status', width: 130, render: (_, item) => <StatusBadge status={item.source_status} /> },
    { title: '自动同步', key: 'auto', width: 110, render: (_, item) => item.auto_sync_enabled ? <Tag color="green">开启</Tag> : <Tag>关闭</Tag> },
    { title: '最近同步', key: 'sync', width: 180, render: (_, item) => formatDateTime(item.last_sync_at) },
    { title: '历史上拉', key: 'history', width: 180, render: (_, item) => formatDateTime(item.last_history_pull_at) },
    { title: '操作', key: 'actions', width: 180, render: (_, item) => canManage ? <Space><Button size="small" onClick={() => sourceRun(item, 'sync')}>同步</Button><Button size="small" onClick={() => sourceRun(item, 'pull-history')}>上拉历史</Button></Space> : '-' },
  ];

  const candidateColumns: ColumnsType<SourceCandidate> = [
    { title: '来源', key: 'target', render: (_, item) => <Space direction="vertical" size={0}><Typography.Text strong>{item.title}</Typography.Text><Typography.Text type="secondary">{item.target_type} / {item.tg_peer_id}</Typography.Text></Space> },
    { title: '可学习', key: 'can_listen', width: 120, render: (_, item) => item.can_listen ? <Tag color="green">可监听</Tag> : <Tag color="orange">{item.cannot_auto_sync_reason || '不可监听'}</Tag> },
    { title: '推荐', key: 'recommended', width: 110, render: (_, item) => item.recommended ? <Tag color="blue">推荐</Tag> : '-' },
    { title: '最近消息', key: 'recent', width: 180, render: (_, item) => formatDateTime(item.recent_message_at) },
  ];

  const sampleColumns: ColumnsType<LearningSample> = [
    { title: '样本', key: 'text', ellipsis: true, render: (_, item) => <Space direction="vertical" size={0}><Typography.Text>{item.text || '-'}</Typography.Text><Typography.Text type="secondary">{item.sender_name || '未知'} / {item.source_scene}</Typography.Text></Space> },
    { title: '分数', dataIndex: 'quality_score', width: 90 },
    { title: '状态', dataIndex: 'learning_status', width: 120 },
    { title: '时间', key: 'sent_at', width: 180, render: (_, item) => formatDateTime(item.sent_at) },
    { title: '操作', key: 'actions', width: 240, render: (_, item) => canManage ? <Space><Button size="small" onClick={() => updateSampleStatus(item, 'accepted')}>采纳</Button><Button size="small" onClick={() => updateSampleStatus(item, 'downweighted')}>降权</Button><Button size="small" danger onClick={() => updateSampleStatus(item, 'rejected')}>剔除</Button></Space> : '-' },
  ];

  const runColumns: ColumnsType<LearningRun> = [
    { title: '类型', dataIndex: 'run_type', width: 130 },
    { title: '状态', key: 'status', width: 110, render: (_, item) => <StatusBadge status={item.status} /> },
    { title: '样本', key: 'samples', width: 130, render: (_, item) => `${item.accepted_count || 0}/${item.sample_count || 0}` },
    { title: '版本', key: 'version', width: 120, render: (_, item) => item.profile_version ? `v${item.profile_version}` : '-' },
    { title: '失败', dataIndex: 'failure_detail', ellipsis: true },
    { title: '时间', key: 'created_at', width: 180, render: (_, item) => formatDateTime(item.created_at) },
  ];

  const versionColumns: ColumnsType<ProfileVersion> = [
    { title: '版本', key: 'version', width: 90, render: (_, item) => `v${item.profile_version}` },
    { title: '状态', key: 'status', width: 120, render: (_, item) => <StatusBadge status={item.status} /> },
    { title: '样本', dataIndex: 'source_sample_count', width: 90 },
    { title: '摘要', dataIndex: 'style_summary', ellipsis: true },
    { title: '创建人', dataIndex: 'created_by', width: 120 },
    { title: '时间', key: 'created_at', width: 180, render: (_, item) => formatDateTime(item.created_at) },
    { title: '操作', key: 'actions', width: 100, render: (_, item) => canManage ? <Button size="small" onClick={() => restoreVersion(item)}>恢复</Button> : '-' },
  ];

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      {error && <Alert type="error" showIcon message={error} />}
      <Card
        className="panel"
        title="全站目标画像"
        extra={<Space><Button icon={<RefreshCcw size={16} />} loading={loading} onClick={load}>刷新</Button>{canManage && <Button loading={saving} onClick={() => updateLearningEnabled(!profile?.learning_enabled)}>{profile?.learning_enabled ? '暂停学习' : '恢复学习'}</Button>}{canManage && <Button type="primary" loading={saving} onClick={rebuildProfile}>重建画像</Button>}{canManage && <Button danger loading={saving} onClick={clearProfile}>清空画像</Button>}</Space>}
      >
        <Descriptions
          bordered
          size="small"
          column={3}
          items={[
            { key: 'version', label: '当前版本', children: `v${profile?.profile_version ?? 0}` },
            { key: 'status', label: '状态', children: <StatusBadge status={profile?.status || '未生成'} /> },
            { key: 'available', label: 'AI 可用', children: profile?.available_for_ai ? <Tag color="green">可用</Tag> : <Tag color="orange">样本不足</Tag> },
            { key: 'scope', label: '使用范围', span: 2, children: (profile?.usage_scope || []).map((item) => TASK_LABELS[item] || item).join(' / ') },
            { key: 'usage', label: '当前使用', children: `${usage?.running_task_count ?? 0} 个任务` },
            { key: 'source', label: '学习位置', children: `${profile?.source_count ?? 0} 个学习来源` },
            { key: 'samples', label: '采纳样本', children: profile?.source_sample_count ?? 0 },
            { key: 'rebuilt', label: '最近重建', children: formatDateTime(profile?.last_rebuilt_at) },
            { key: 'summary', label: '画像摘要', span: 3, children: profile?.style_summary || '暂无画像摘要，配置来源并采纳样本后可重建。' },
          ]}
        />
      </Card>

      <Card className="panel" title="学习来源" extra={<Space><Button onClick={onOpenTargets}>查看运营目标</Button>{canManage && <Button type="primary" loading={saving} onClick={saveSelectedSources}>保存选中来源</Button>}</Space>}>
        <Table<LearningSource> rowKey="id" size="small" loading={loading} dataSource={sources} columns={sourceColumns} pagination={false} locale={{ emptyText: <Empty description="还没有选择学习来源" /> }} />
      </Card>

      <Card className="panel" title="来源候选" extra={canManage ? <Button loading={saving} onClick={recomputeCandidates}>重算候选</Button> : null}>
        <Table<SourceCandidate>
          rowKey={candidateKey}
          size="small"
          loading={loading}
          dataSource={candidates}
          columns={candidateColumns}
          pagination={{ pageSize: 8 }}
          rowSelection={canManage ? {
            selectedRowKeys: selectedSourceIds,
            getCheckboxProps: (item) => ({ disabled: !item.can_listen }),
            onChange: setSelectedSourceIds,
          } : undefined}
        />
      </Card>

      <Card className="panel" title="样本质量过滤规则" extra={canManage ? <Button icon={<ShieldAlert size={16} />} loading={saving} onClick={saveQualityRules}>保存规则</Button> : null}>
        <Form form={form} layout="vertical" initialValues={ruleToForm(qualityRule)}>
          <div className="form-grid">
            <Form.Item name="exclude_bots" label="过滤机器人" valuePropName="checked"><Switch /></Form.Item>
            <Form.Item name="exclude_managed_accounts" label="过滤托管账号" valuePropName="checked"><Switch /></Form.Item>
            <Form.Item name="min_length" label="最短文本"><InputNumber min={0} style={{ width: '100%' }} /></Form.Item>
            <Form.Item name="max_length" label="最长文本"><InputNumber min={1} style={{ width: '100%' }} /></Form.Item>
            <Form.Item name="accepted" label="采纳分阈值"><InputNumber min={0} max={100} style={{ width: '100%' }} /></Form.Item>
            <Form.Item name="downweighted" label="降权分阈值"><InputNumber min={0} max={100} style={{ width: '100%' }} /></Form.Item>
            <Form.Item name="similarity_threshold" label="模板相似阈值"><InputNumber min={0} max={1} step={0.01} style={{ width: '100%' }} /></Form.Item>
            <Form.Item name="group_chat_weight" label="群聊场景权重"><InputNumber min={0} step={0.1} style={{ width: '100%' }} /></Form.Item>
            <Form.Item name="channel_comment_weight" label="频道评论权重"><InputNumber min={0} step={0.1} style={{ width: '100%' }} /></Form.Item>
            <Form.Item name="discussion_reply_weight" label="讨论回复权重"><InputNumber min={0} step={0.1} style={{ width: '100%' }} /></Form.Item>
            <Form.Item name="forbidden_mode" label="禁学模式"><Select options={[{ label: '命中即剔除', value: 'reject' }, { label: '命中后降权', value: 'downweight' }]} /></Form.Item>
            <Form.Item name="links" label="过滤链接" valuePropName="checked"><Switch /></Form.Item>
            <Form.Item name="contacts" label="过滤联系方式" valuePropName="checked"><Switch /></Form.Item>
            <Form.Item name="keywords" label="必须包含关键词"><Select mode="tags" open={false} tokenSeparators={[',', '，']} /></Form.Item>
            <Form.Item name="forbidden_keywords" label="禁止关键词"><Select mode="tags" open={false} tokenSeparators={[',', '，']} /></Form.Item>
            <Form.Item name="phrases" label="模板短语"><Select mode="tags" open={false} tokenSeparators={[',', '，']} /></Form.Item>
          </div>
          <Typography.Text type="secondary">当前规则版本 v{qualityRule?.rule_version ?? 0}，更新人 {qualityRule?.updated_by || '-'}，更新时间 {formatDateTime(qualityRule?.updated_at)}。</Typography.Text>
        </Form>
      </Card>

      <Card className="panel" title="学习样本">
        <Table<LearningSample> rowKey="id" size="small" loading={loading} dataSource={samples} columns={sampleColumns} pagination={{ pageSize: 8 }} />
      </Card>

      <Card className="panel" title="画像版本">
        <Table<ProfileVersion> rowKey="id" size="small" loading={loading} dataSource={versions} columns={versionColumns} pagination={{ pageSize: 8 }} />
      </Card>

      <Card className="panel" title="学习运行记录">
        <Table<LearningRun> rowKey="id" size="small" loading={loading} dataSource={runs} columns={runColumns} pagination={{ pageSize: 8 }} />
      </Card>
    </Space>
  );
}
