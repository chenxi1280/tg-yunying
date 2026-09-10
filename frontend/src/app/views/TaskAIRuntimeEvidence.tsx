import React from 'react';
import { Space, Tag, Typography } from 'antd';
import type { AIRuntimeDiagnostics } from '../types/aiRuntimeDiagnostics';

const OUTCOME_LABELS: Record<string, string> = {
  remote_confirmed: '可见消息已确认',
  telegram_unknown: '发送结果未知',
  admission_blocked: '准入终态阻塞',
  receipt_without_visible_fact: '已有回执，待可见事实',
  content_ready: '正文已准备',
  emergency_pending: '应急生成待处理',
  provider_unknown: '模型返回未知',
  generation_failed: '当前生成失败',
  generation_pending: '生成处理中',
  terminal_failed: '未完成终态',
};

const QUEUE_LABELS: Record<string, string> = {
  valid_wait: '原期限内等待',
  outside_original_deadline: '原期限内无可执行时刻',
  expired_uncalled: '已过原期限且未调用',
  called_history: '已有调用记录，保留原工作核对',
  unknown_preserved: '未知结果保留',
  terminal: '已收口',
};

function CountTags({ counts, labels }: { counts: Record<string, number>; labels: Record<string, string> }) {
  return <Space wrap>{Object.entries(counts).map(([key, count]) => (
    <Tag key={key}>{labels[key] || key} {count}</Tag>
  ))}</Space>;
}

export function TaskAIRuntimeEvidence({ runtime }: { runtime?: AIRuntimeDiagnostics }) {
  const outcomes = runtime?.generation_outcomes;
  const queue = runtime?.original_deadline_queue;
  if (!outcomes) return null;
  return <Space direction="vertical" size={6} style={{ width: '100%' }}>
    <Typography.Text strong>本轮生命周期的工作结果：{outcomes.work_count} 条</Typography.Text>
    <CountTags counts={outcomes.outcome_counts} labels={OUTCOME_LABELS} />
    <Typography.Text type="secondary">
      按当前义务去重；普通生成失败后转应急的后续结果单独归类。阶段统计不等于发送失败数。
    </Typography.Text>
    {queue?.status === 'observed' && <>
      <Typography.Text>原任务日排期</Typography.Text>
      <Typography.Text type="secondary">原期限截止：{queue.original_deadline_at ? new Date(queue.original_deadline_at).toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' }) : '未知'}</Typography.Text>
      <CountTags counts={queue.state_counts} labels={QUEUE_LABELS} />
    </>}
  </Space>;
}
