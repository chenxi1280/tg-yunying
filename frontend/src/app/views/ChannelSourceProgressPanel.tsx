import React from 'react';
import { Card, Descriptions } from 'antd';

const sourceLabels: Record<string, string> = {
  sources_available: '有可用来源', waiting_no_opportunity: '等待来源机会',
  neutral_no_opportunity: '当日无来源机会', missed_no_source: '有限来源不足',
  missed_promised_source: '当日应有来源但未观察到', source_ingestion_unproven: '来源观察不完整',
  source_capability_blocked: '来源已发布，但评论能力受限',
};

export function ChannelSourceProgressPanel({ stats }: { stats: Record<string, any> }) {
  const source = stats.source_intake;
  const album = stats.album_reactions;
  if (!source && !album) return null;
  return <Card size="small" title="来源与相册履约">
    <Descriptions size="small" column={2} items={[
      ...(source ? [
        { key: 'state', label: '来源状态', children: sourceLabels[source.state] || source.state },
        { key: 'initial', label: '首次历史来源', children: source.initial_source_count },
        { key: 'filtered', label: '已过滤非正文', children: source.counts?.source_filtered_non_content || 0 },
        { key: 'archived', label: '范围外历史', children: source.counts?.source_archived_skipped || 0 },
        { key: 'capability', label: '评论能力受限来源', children: source.capability_blocked_count || 0 },
      ] : []),
      ...(album ? [
        { key: 'accounts', label: '相册目标账号义务', children: album.configured_distinct_accounts ?? '未取得目标快照' },
        { key: 'confirmed', label: '全部子操作确认的账号', children: album.confirmed_accounts },
        { key: 'planned', label: '计划子操作次数', children: album.planned_child_rpc },
        { key: 'children', label: '确认子操作次数', children: album.confirmed_child_reactions },
        { key: 'partial', label: '部分确认账号', children: album.partial_accounts },
        { key: 'unknown', label: '结果未知子操作', children: album.unknown_children },
      ] : []),
    ]} />
  </Card>;
}
