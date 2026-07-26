import React from 'react';
import { Button, Space, Table, Tag, Typography } from 'antd';
import { api } from '../../shared/api/client';
import type {
  AiAccountVoiceProfile,
  AiAccountVoiceProfileGenerationItem,
  AiAccountVoiceProfileGenerationJob,
} from '../types';

interface Options {
  canManage: boolean;
  selectedAccountIds: number[];
  clearSelectedAccountIds: () => void;
  onError: (value: string) => void;
  onNotice: (value: string) => void;
  refreshProfiles: () => Promise<void>;
}

interface QueueRequest {
  prefix: string;
  mode: 'selected' | 'missing';
  accountIds: number[];
  rebuildExisting: boolean;
}

interface Context {
  options: Options;
  job: AiAccountVoiceProfileGenerationJob | null;
  setJob: React.Dispatch<React.SetStateAction<AiAccountVoiceProfileGenerationJob | null>>;
  setSavingKey: React.Dispatch<React.SetStateAction<string>>;
}

interface JobPanelProps {
  canManage: boolean;
  job: AiAccountVoiceProfileGenerationJob;
  savingKey: string;
  onRetry: (item: AiAccountVoiceProfileGenerationItem) => void;
}

interface GenerationJobListResponse {
  items: Array<Omit<AiAccountVoiceProfileGenerationJob, 'items'>>;
}

function errorText(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}

export function generationStatusTag(status: string) {
  if (status === 'succeeded') return <Tag color="green">已完成</Tag>;
  if (status === 'queued') return <Tag color="blue">已入队</Tag>;
  if (status === 'generating' || status === 'validating') return <Tag color="processing">生成中</Tag>;
  if (status === 'retry_wait') return <Tag color="orange">等待重试</Tag>;
  if (status === 'manual_required') return <Tag color="red">需人工处理</Tag>;
  if (status === 'skipped') return <Tag color="default">已跳过</Tag>;
  return <Tag>{status || '-'}</Tag>;
}

function generationIdempotencyKey(prefix: string) {
  return `${prefix}:${Date.now()}:${Math.random().toString(36).slice(2)}`;
}

function generationNotice(prefix: string, job: AiAccountVoiceProfileGenerationJob) {
  return `${prefix}已入队：共 ${job.total_count} 个账号，当前状态 ${job.status || 'queued'}`;
}

async function loadLatestGenerationJob() {
  const result = await api<GenerationJobListResponse>('/ai-account-voice-profile-generation-jobs?limit=1');
  const latest = result.items[0];
  if (!latest) return null;
  return api<AiAccountVoiceProfileGenerationJob>(`/ai-account-voice-profile-generation-jobs/${latest.id}`);
}

async function runGenerationAction(context: Context, key: string, action: () => Promise<void>) {
  context.setSavingKey(key);
  context.options.onError('');
  try {
    await action();
  } catch (error) {
    context.options.onError(errorText(error));
  } finally {
    context.setSavingKey('');
  }
}

async function submitGenerationJob(context: Context, request: QueueRequest) {
  const job = await api<AiAccountVoiceProfileGenerationJob>('/ai-account-voice-profile-generation-jobs', {
    method: 'POST',
    body: JSON.stringify({
      mode: request.mode,
      account_ids: request.accountIds,
      rebuild_existing: request.rebuildExisting,
      reason: request.prefix,
      idempotency_key: generationIdempotencyKey(request.prefix),
    }),
  });
  context.setJob(job);
  context.options.onNotice(generationNotice(request.prefix, job));
  await context.options.refreshProfiles();
}

function rebuildProfile(context: Context, profile: AiAccountVoiceProfile) {
  return runGenerationAction(context, `rebuild:${profile.account_id}`, () => submitGenerationJob(context, {
    prefix: '账号面具重建', mode: 'selected', accountIds: [profile.account_id], rebuildExisting: true,
  }));
}

function batchRebuildMissing(context: Context) {
  return runGenerationAction(context, 'batch-rebuild-missing', () => submitGenerationJob(context, {
    prefix: '批量补齐缺面具账号', mode: 'missing', accountIds: [], rebuildExisting: false,
  }));
}

function batchRebuildSelected(context: Context) {
  return runGenerationAction(context, 'batch-rebuild-selected', async () => {
    await submitGenerationJob(context, {
      prefix: '批量重建账号面具', mode: 'selected', accountIds: context.options.selectedAccountIds, rebuildExisting: true,
    });
    context.options.clearSelectedAccountIds();
  });
}

function refreshGenerationJob(context: Context) {
  if (!context.job) return Promise.resolve();
  return runGenerationAction(context, 'refresh-generation-job', async () => {
    const job = await api<AiAccountVoiceProfileGenerationJob>(`/ai-account-voice-profile-generation-jobs/${context.job?.id}`);
    context.setJob(job);
    await context.options.refreshProfiles();
  });
}

function retryGenerationItem(context: Context, item: AiAccountVoiceProfileGenerationItem) {
  if (!context.job) return Promise.resolve();
  return runGenerationAction(context, `retry-generation:${item.id}`, async () => {
    const result = await api<{ job: AiAccountVoiceProfileGenerationJob; item: AiAccountVoiceProfileGenerationItem }>(
      `/ai-account-voice-profile-generation-items/${item.id}/retry`,
      {
        method: 'POST',
        body: JSON.stringify({
          reason: '运营立即重试账号面具生成',
          expected_status: item.status,
          expected_profile_version: item.expected_profile_version,
          idempotency_key: generationIdempotencyKey(`retry-${item.id}`),
        }),
      },
    );
    context.setJob(result.job);
    context.options.onNotice(`账号 #${item.account_id} 已重新入队`);
    await context.options.refreshProfiles();
  });
}

function generationItemColumns(props: JobPanelProps) {
  return [
    { title: '账号', dataIndex: 'account_id', width: 100, render: (value: number) => `#${value}` },
    { title: '履约状态', dataIndex: 'status', width: 110, render: (status: string) => generationStatusTag(status) },
    { title: '尝试', dataIndex: 'attempt_count', width: 70 },
    { title: '下次重试', width: 160, render: (_: unknown, item: AiAccountVoiceProfileGenerationItem) => item.next_retry_at ? item.next_retry_at.replace('T', ' ').slice(0, 16) : '-' },
    { title: '错误', dataIndex: 'error_code', width: 180, render: (value: string) => value || '-' },
    { title: '错误摘要', dataIndex: 'error_detail', ellipsis: true, render: (value: string) => value || '-' },
    {
      title: '操作',
      width: 110,
      render: (_: unknown, item: AiAccountVoiceProfileGenerationItem) => (
        <Button
          size="small"
          disabled={!props.canManage || !['retry_wait', 'manual_required'].includes(item.status)}
          loading={props.savingKey === `retry-generation:${item.id}`}
          onClick={() => props.onRetry(item)}
        >
          立即重试
        </Button>
      ),
    },
  ];
}

function GenerationJobPanel(props: JobPanelProps) {
  return (
    <Space direction="vertical" size={8} style={{ width: '100%' }}>
      <Typography.Title level={5} style={{ margin: 0 }}>账号面具生成任务：{generationStatusTag(props.job.status)}</Typography.Title>
      <Table
        rowKey="id"
        size="small"
        dataSource={props.job.items}
        columns={generationItemColumns(props)}
        pagination={false}
        scroll={{ x: 900 }}
      />
    </Space>
  );
}

export function useVoiceProfileGeneration(options: Options) {
  const [job, setJob] = React.useState<AiAccountVoiceProfileGenerationJob | null>(null);
  const [savingKey, setSavingKey] = React.useState('');
  React.useEffect(() => {
    let mounted = true;
    void loadLatestGenerationJob()
      .then((latest) => { if (mounted && latest) setJob(latest); })
      .catch((error) => { if (mounted) options.onError(errorText(error)); });
    return () => { mounted = false; };
  }, []);
  const context = { options, job, setJob, setSavingKey };
  const panel = job ? (
    <GenerationJobPanel
      canManage={options.canManage}
      job={job}
      savingKey={savingKey}
      onRetry={(item) => { void retryGenerationItem(context, item); }}
    />
  ) : null;
  return {
    job,
    panel,
    savingKey,
    rebuildProfile: (profile: AiAccountVoiceProfile) => rebuildProfile(context, profile),
    batchRebuildMissing: () => batchRebuildMissing(context),
    batchRebuildSelected: () => batchRebuildSelected(context),
    refreshJob: () => refreshGenerationJob(context),
  };
}
