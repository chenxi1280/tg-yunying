import React from 'react';
import { Descriptions, Space, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { TaskCenterDetail } from '../types';

type QualitySample = NonNullable<NonNullable<TaskCenterDetail['ai_quality_funnel']['samples']>[string]>[number];

const REASON_LABELS: Record<string, string> = {
  duplicate_message: '重复拦截',
  template_shell_limited: '模板壳限频',
  profile_low_match: '画像低分',
  voice_profile_mismatch: '面具低分',
  stance_conflict: '立场冲突',
  account_offline: '账号离线',
  context_insufficient: '上下文不足',
  quality_fallback: '质量兜底',
  hallucination_risk: '事实锚点不足',
  diversity_penalty: '同批多样性降权',
};

function reasonLabel(reason: string) {
  return REASON_LABELS[reason] || reason;
}

function sampleRows(funnel: TaskCenterDetail['ai_quality_funnel']) {
  const samples = funnel.samples || {};
  return Object.entries(samples).flatMap(([reason, rows]) => (rows || []).map((row) => ({ ...row, reason })));
}

export function TaskAIQualityFunnelPanel({ funnel }: { funnel: TaskCenterDetail['ai_quality_funnel'] }) {
  const totals = funnel.totals || {};
  const reasonCounts = funnel.reason_counts || {};
  const rows = sampleRows(funnel);
  const sampleColumns: ColumnsType<QualitySample & { reason: string }> = [
    { title: '原因', dataIndex: 'reason', width: 130, render: (reason) => <Tag>{reasonLabel(String(reason))}</Tag> },
    { title: '账号', dataIndex: 'account_id', width: 90, render: (value) => value || '-' },
    { title: '状态', dataIndex: 'status', width: 100 },
    { title: '内容样例', dataIndex: 'content', ellipsis: true },
    { title: '细节', dataIndex: 'detail', width: 180, ellipsis: true, render: (value) => value || '-' },
  ];
  if (!totals.action_count && !totals.candidate_count) return null;
  return (
    <Space direction="vertical" size={8} style={{ width: '100%' }}>
      <Descriptions
        bordered
        size="small"
        column={4}
        items={[
          { key: 'candidate', label: '候选数', children: totals.candidate_count || 0 },
          { key: 'passed', label: '通过文本', children: totals.passed_count || 0 },
          { key: 'sent', label: '最终发送', children: totals.final_send_count || 0 },
          { key: 'actions', label: '动作数', children: totals.action_count || 0 },
        ]}
      />
      <Space wrap>
        {Object.entries(reasonCounts).filter(([, count]) => Number(count) > 0).map(([reason, count]) => (
          <Tag key={reason} color={reason === 'quality_fallback' ? 'orange' : 'blue'}>{reasonLabel(reason)} {count}</Tag>
        ))}
      </Space>
      {rows.length > 0 && (
        <>
          <Typography.Text type="secondary">代表样例</Typography.Text>
          <Table rowKey={(row) => `${row.reason}:${row.action_id}`} columns={sampleColumns} dataSource={rows} pagination={false} size="small" scroll={{ x: 760 }} />
        </>
      )}
    </Space>
  );
}
