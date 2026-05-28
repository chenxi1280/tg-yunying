import React from 'react';
import { Alert, App as AntdApp, Button, Card, Descriptions, Empty, Form, Input, InputNumber, Select, Space, Switch, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { RefreshCcw, ShieldAlert } from 'lucide-react';
import { api, ApiError } from '../../shared/api/client';
import { StatusBadge } from '../components/shared';
import { formatBeijingDateTime } from '../time';

type Props = {
  canManage: boolean;
  onOpenTargets: () => void;
};

type TargetProfileOverview = {
  profile_id: string;
  profile_version: number;
  status: string;
  learning_enabled: boolean;
  usage_scope: string[];
  style_summary: string;
  source_sample_count: number;
  source_count: number;
  quality_rule_version: number;
  last_rebuilt_at?: string | null;
  available_for_ai: boolean;
};

type TargetProfileUsage = {
  running_task_count: number;
  task_type_distribution: Record<string, number>;
};

type SourceCandidate = {
  source_key: string;
  group_id?: number | null;
  target_id?: number | null;
  target_type: string;
  title: string;
  tg_peer_id: string;
  can_listen: boolean;
  listener_account_ids: number[];
  recent_message_at?: string | null;
  recommended: boolean;
  recommend_reason: string;
  cannot_auto_sync_reason: string;
};

type LearningSource = {
  id: string;
  target_id: number;
  target_title: string;
  target_type: string;
  is_enabled: boolean;
  auto_sync_enabled: boolean;
  source_status: string;
  listener_account_ids: number[];
  last_sync_at?: string | null;
  last_history_pull_at?: string | null;
  last_failure_detail: string;
};

type LearningSample = {
  id: string;
  source_scene: string;
  sender_name: string;
  text: string;
  learning_status: string;
  quality_score: number;
  sent_at?: string | null;
};

type LearningRun = {
  id: string;
  run_type: string;
  status: string;
  pulled_count: number;
  sample_count: number;
  accepted_count: number;
  rejected_count: number;
  profile_version?: number | null;
  quality_rule_version?: number | null;
  failure_detail: string;
  created_at?: string | null;
};

type ProfileVersion = {
  id: string;
  profile_version: number;
  status: string;
  style_summary: string;
  source_sample_count: number;
  quality_rule_version: number;
  created_by: string;
  created_at?: string | null;
};

type QualityRule = {
  rule_version: number;
  identity_filters: Record<string, any>;
  text_filters: Record<string, any>;
  template_filters: Record<string, any>;
  scoring_thresholds: Record<string, any>;
  forbidden_patterns: Record<string, any>;
  updated_by: string;
  updated_at?: string | null;
};

type QualityRuleForm = {
  exclude_bots: boolean;
  exclude_managed_accounts: boolean;
  min_length: number;
  max_length: number;
  keywords: string[];
  similarity_threshold: number;
  phrases: string[];
  accepted: number;
  downweighted: number;
  forbidden_keywords: string[];
  links: boolean;
  contacts: boolean;
};

const TASK_LABELS: Record<string, string> = {
  group_ai_chat: 'AI 活群',
  channel_comment: '频道评论',
  discussion_reply: '回复',
};

function formatDateTime(value?: string | null) {
  return formatBeijingDateTime(value);
}

function errorMessage(error: unknown) {
  if (error instanceof ApiError) {
    try {
      const parsed = JSON.parse(error.body) as { detail?: unknown };
      if (typeof parsed.detail === 'string') return parsed.detail;
    } catch {
      return error.body || error.message;
    }
    return error.body || error.message;
  }
  return error instanceof Error ? error.message : String(error);
}

function ruleToForm(rule: QualityRule | null): QualityRuleForm {
  return {
    exclude_bots: Boolean(rule?.identity_filters?.exclude_bots ?? true),
    exclude_managed_accounts: Boolean(rule?.identity_filters?.exclude_managed_accounts ?? true),
    min_length: Number(rule?.text_filters?.min_length ?? 2),
    max_length: Number(rule?.text_filters?.max_length ?? 4000),
    keywords: Array.isArray(rule?.text_filters?.keywords) ? rule.text_filters.keywords : [],
    similarity_threshold: Number(rule?.template_filters?.similarity_threshold ?? 0.92),
    phrases: Array.isArray(rule?.template_filters?.phrases) ? rule.template_filters.phrases : [],
    accepted: Number(rule?.scoring_thresholds?.accepted ?? 80),
    downweighted: Number(rule?.scoring_thresholds?.downweighted ?? 40),
    forbidden_keywords: Array.isArray(rule?.forbidden_patterns?.keywords) ? rule.forbidden_patterns.keywords : [],
    links: Boolean(rule?.forbidden_patterns?.links ?? true),
    contacts: Boolean(rule?.forbidden_patterns?.contacts ?? true),
  };
}

function formToRule(values: QualityRuleForm, reason: string) {
  return {
    reason,
    identity_filters: {
      exclude_bots: values.exclude_bots,
      exclude_managed_accounts: values.exclude_managed_accounts,
    },
    text_filters: {
      min_length: values.min_length,
      max_length: values.max_length,
      keywords: values.keywords ?? [],
    },
    template_filters: {
      similarity_threshold: values.similarity_threshold,
      phrases: values.phrases ?? [],
    },
    scoring_thresholds: {
      accepted: values.accepted,
      downweighted: values.downweighted,
    },
    forbidden_patterns: {
      keywords: values.forbidden_keywords ?? [],
      links: values.links,
      contacts: values.contacts,
    },
  };
}

function candidateKey(item: SourceCandidate) {
  return item.source_key || String(item.target_id ?? item.group_id ?? item.tg_peer_id);
}

function selectedSourceKeys(sources: LearningSource[], candidates: SourceCandidate[]): React.Key[] {
  const activeTargetIds = new Set(sources.filter((item) => item.is_enabled).map((item) => item.target_id));
  const activeKeys = candidates.filter((item) => item.target_id && activeTargetIds.has(item.target_id)).map(candidateKey);
  if (activeKeys.length) return activeKeys;
  return candidates.filter((item) => item.recommended && item.can_listen).map(candidateKey);
}

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

  async function load() {
    setLoading(true);
    setError('');
    try {
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
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }

  React.useEffect(() => {
    void load();
  }, []);

  function requireReason(prompt: string) {
    const reason = window.prompt(prompt);
    return reason?.trim() || '';
  }

  async function runAction(action: () => Promise<void>, successText: string) {
    setSaving(true);
    setError('');
    try {
      await action();
      void message.success(successText);
      await load();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  async function saveSelectedSources() {
    const selectedKeySet = new Set(selectedSourceIds.map((item) => String(item)));
    const selected = candidates.filter((item) => selectedKeySet.has(candidateKey(item)));
    const reason = requireReason('填写保存学习来源的原因');
    if (!reason) return;
    await runAction(async () => {
      await api('/target-profile/sources', {
        method: 'PUT',
        body: JSON.stringify({
          reason,
          sources: selected.map((item) => ({
            group_id: item.group_id,
            target_id: item.target_id,
            is_enabled: true,
            auto_sync_enabled: true,
            listener_account_ids: item.listener_account_ids,
          })),
        }),
      });
    }, `已保存 ${selected.length} 个学习来源`);
  }

  async function sourceRun(source: LearningSource, path: 'sync' | 'pull-history') {
    await runAction(async () => {
      await api(`/target-profile/sources/${source.id}/${path}`, { method: 'POST' });
    }, path === 'sync' ? '已同步来源' : '已开始上拉历史');
  }

  async function saveQualityRules() {
    const reason = requireReason('填写质量过滤规则变更原因');
    if (!reason) return;
    const values = await form.validateFields();
    await runAction(async () => {
      await api('/target-profile/quality-rules', {
        method: 'PATCH',
        body: JSON.stringify(formToRule(values, reason)),
      });
    }, '已保存样本质量规则');
  }

  async function updateSampleStatus(sample: LearningSample, learningStatus: string) {
    const reason = requireReason('填写样本状态调整原因');
    if (!reason) return;
    await runAction(async () => {
      await api(`/target-profile/samples/${sample.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ learning_status: learningStatus, reason }),
      });
    }, '已调整样本状态');
  }

  async function rebuildProfile() {
    const reason = requireReason('填写重建全站目标画像的原因');
    if (!reason) return;
    await runAction(async () => {
      await api('/target-profile/rebuild', { method: 'POST', body: JSON.stringify({ reason }) });
    }, '已重建全站目标画像');
  }

  async function clearProfile() {
    const reason = requireReason('填写清空全站目标画像的原因');
    if (!reason) return;
    await runAction(async () => {
      await api('/target-profile/clear', { method: 'POST', body: JSON.stringify({ reason }) });
    }, '已清空全站目标画像');
  }

  async function updateLearningEnabled(learningEnabled: boolean) {
    const reason = requireReason(learningEnabled ? '填写恢复画像学习的原因' : '填写暂停画像学习的原因');
    if (!reason) return;
    await runAction(async () => {
      await api('/target-profile/settings', {
        method: 'PATCH',
        body: JSON.stringify({ learning_enabled: learningEnabled, reason }),
      });
    }, learningEnabled ? '已恢复画像学习' : '已暂停画像学习');
  }

  async function restoreVersion(version: ProfileVersion) {
    const reason = requireReason(`填写恢复 v${version.profile_version} 的原因`);
    if (!reason) return;
    await runAction(async () => {
      await api(`/target-profile/versions/${version.id}/restore`, {
        method: 'POST',
        body: JSON.stringify({ reason }),
      });
    }, `已恢复 v${version.profile_version}`);
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
    { title: '群聊', key: 'target', render: (_, item) => <Space direction="vertical" size={0}><Typography.Text strong>{item.title}</Typography.Text><Typography.Text type="secondary">{item.target_type} / {item.tg_peer_id}</Typography.Text></Space> },
    { title: '可学习', key: 'can_listen', width: 120, render: (_, item) => item.can_listen ? <Tag color="green">可监听</Tag> : <Tag color="orange">{item.cannot_auto_sync_reason || '不可监听'}</Tag> },
    { title: '推荐', key: 'recommended', width: 110, render: (_, item) => item.recommended ? <Tag color="blue">推荐</Tag> : '-' },
    { title: '最近消息', key: 'recent', width: 180, render: (_, item) => formatDateTime(item.recent_message_at) },
  ];

  const sampleColumns: ColumnsType<LearningSample> = [
    { title: '样本', key: 'text', ellipsis: true, render: (_, item) => <Space direction="vertical" size={0}><Typography.Text>{item.text || '-'}</Typography.Text><Typography.Text type="secondary">{item.sender_name || '未知'} / {item.source_scene}</Typography.Text></Space> },
    { title: '分数', dataIndex: 'quality_score', width: 90 },
    { title: '状态', dataIndex: 'learning_status', width: 120 },
    { title: '时间', key: 'sent_at', width: 180, render: (_, item) => formatDateTime(item.sent_at) },
    { title: '操作', key: 'actions', width: 180, render: (_, item) => canManage ? <Space><Button size="small" onClick={() => updateSampleStatus(item, 'accepted')}>采纳</Button><Button size="small" danger onClick={() => updateSampleStatus(item, 'rejected')}>剔除</Button></Space> : '-' },
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
            { key: 'source', label: '学习位置', children: `${profile?.source_count ?? 0} 个群聊来源` },
            { key: 'samples', label: '采纳样本', children: profile?.source_sample_count ?? 0 },
            { key: 'rebuilt', label: '最近重建', children: formatDateTime(profile?.last_rebuilt_at) },
            { key: 'summary', label: '画像摘要', span: 3, children: profile?.style_summary || '暂无画像摘要，配置来源并采纳样本后可重建。' },
          ]}
        />
      </Card>

      <Card className="panel" title="学习来源" extra={<Space><Button onClick={onOpenTargets}>查看运营目标</Button>{canManage && <Button type="primary" loading={saving} onClick={saveSelectedSources}>保存选中来源</Button>}</Space>}>
        <Table<LearningSource> rowKey="id" size="small" loading={loading} dataSource={sources} columns={sourceColumns} pagination={false} locale={{ emptyText: <Empty description="还没有选择学习来源" /> }} />
      </Card>

      <Card className="panel" title="群聊候选">
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
