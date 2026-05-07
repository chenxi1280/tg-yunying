import React from 'react';
import { Archive, MessageSquareText } from 'lucide-react';
import { Button, Card, Descriptions, List, Space, Typography } from 'antd';
import type { Group } from '../types';
import { StatusBadge } from '../components/shared';
import { statusAccent, operationLabel } from '../utils';

interface Props {
  groups: Group[];
  selectedGroup: Group | undefined;
  selectedGroupId: number | null;
  setSelectedGroupId: (id: number | null) => void;
  onCreateCampaign: (groupId?: number) => void;
  onCreateArchive: () => void;
  onAuthorizeGroup: (status: string) => void;
  onEditGroupPolicy: () => void;
  onOpenConfirm: (payload: {
    title: string;
    message: string;
    confirmLabel: string;
    tone?: 'normal' | 'danger';
    onConfirm: () => void | Promise<void>;
  }) => void;
}

export default function GroupsView({
  groups,
  selectedGroup,
  selectedGroupId,
  setSelectedGroupId,
  onCreateCampaign,
  onCreateArchive,
  onAuthorizeGroup,
  onEditGroupPolicy,
  onOpenConfirm,
}: Props) {
  return (
    <section className="split-view">
      <Card className="panel" title="群聊库" extra={<Typography.Text type="secondary">按可运营、仅归档和不可操作来管理群聊使用范围</Typography.Text>}>
        <List
          className="group-list"
          dataSource={groups}
          renderItem={(group) => (
            <List.Item
              className={`${selectedGroupId === group.id ? 'selected group-card' : 'group-card'} ${statusAccent(group.auth_status)}`}
              onClick={() => setSelectedGroupId(group.id)}
              actions={[<StatusBadge status={group.auth_status} label={operationLabel(group.auth_status)} />]}
            >
              <List.Item.Meta title={group.title} description={`${group.member_count.toLocaleString()} 成员 / ${group.group_type}`} />
            </List.Item>
          )}
        />
      </Card>
      {selectedGroup && (
        <Card className="panel detail-panel" title={selectedGroup.title} extra={<Typography.Text type="secondary">{selectedGroup.topic_direction}</Typography.Text>}>
          <Descriptions className="detail-list" column={2} size="small" items={[
            { key: 'active_window', label: '活跃时间', children: selectedGroup.active_window },
            { key: 'daily_limit', label: '每日上限', children: `${selectedGroup.daily_limit} 条` },
            { key: 'review', label: '审核策略', children: <StatusBadge status={selectedGroup.require_review ? '待审核' : '已审核'} label={selectedGroup.require_review ? '需要人工审核' : '规则内自动'} /> },
            { key: 'can_send', label: '可发言', children: <StatusBadge status={selectedGroup.can_send ? '可发言' : '不可发言'} /> },
            { key: 'listener', label: '监听续聊', children: <StatusBadge status={selectedGroup.listener_enabled ? '已启用' : '未配置'} label={selectedGroup.listener_enabled ? `${selectedGroup.listener_account_ids.length} 个监听号` : '未启用'} /> },
            { key: 'auto_reply', label: '自动发送', children: selectedGroup.listener_auto_reply_enabled ? '监听触发后自动排队' : '只采集上下文' },
          ]} />
          {selectedGroup.listener_last_error && <Typography.Paragraph type="danger">{selectedGroup.listener_last_error}</Typography.Paragraph>}
          <Space className="detail-actions" wrap>
            <Button type="primary" icon={<MessageSquareText size={18} />} onClick={() => onCreateCampaign(selectedGroup.id)}>用此群创建任务</Button>
            <Button icon={<Archive size={18} />} onClick={() => onOpenConfirm({
              title: '创建群归档',
              message: `确认归档「${selectedGroup.title}」的消息与成员清单？`,
              confirmLabel: '创建归档',
              onConfirm: onCreateArchive,
            })}>创建归档</Button>
            {['已授权运营', '只读归档', '禁止操作'].map((status) => (
              <Button key={status} danger={status === '禁止操作'} onClick={() => onOpenConfirm({
                title: '更新群使用范围',
                message: `确认将「${selectedGroup.title}」设置为「${operationLabel(status)}」？`,
                confirmLabel: '确认更新',
                tone: status === '禁止操作' ? 'danger' : 'normal',
                onConfirm: () => onAuthorizeGroup(status),
              })}>{operationLabel(status)}</Button>
            ))}
          </Space>
          <Card className="sub-panel" title="运营配置" extra={<Button size="small" onClick={onEditGroupPolicy}>编辑运营配置</Button>}>
            <div className="summary-grid">
              <Card className="summary-card" size="small"><span>冷却规则</span><strong>账号 {selectedGroup.account_cooldown_seconds}s</strong><p>群 {selectedGroup.group_cooldown_seconds}s</p></Card>
              <Card className="summary-card" size="small"><span>内容边界</span><strong>{selectedGroup.banned_words || '未配置禁用词'}</strong><p>{selectedGroup.link_whitelist || '未配置链接白名单'}</p></Card>
              <Card className="summary-card" size="small"><span>话题方向</span><strong>{selectedGroup.topic_direction || '未设置'}</strong><p>{selectedGroup.require_review ? '需要人工审核' : '规则内自动'}</p></Card>
              <Card className="summary-card" size="small"><span>监听配置</span><strong>{selectedGroup.listener_enabled ? `${selectedGroup.listener_interval_seconds}s 轮询` : '未启用'}</strong><p>上下文 {selectedGroup.listener_context_limit} 条</p></Card>
            </div>
          </Card>
        </Card>
      )}
    </section>
  );
}
