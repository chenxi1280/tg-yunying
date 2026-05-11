import React from 'react';
import { Archive, MessageSquareText } from 'lucide-react';
import { Button, Card, Descriptions, List, Modal, Space, Typography } from 'antd';
import type { Group } from '../types';
import { StatusBadge } from '../components/shared';
import { statusAccent, operationLabel } from '../utils';

interface Props {
  groups: Group[];
  selectedGroup: Group | undefined;
  selectedGroupId: number | null;
  setSelectedGroupId: (id: number | null) => void;
  onCreateTask: (groupId?: number) => void;
  onCreateArchive: () => void;
  onAuthorizeGroup: (status: string) => void;
  onEditGroupPolicy: () => void;
  isActionPending: (key: string) => boolean;
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
  selectedGroupId,
  setSelectedGroupId,
  onCreateTask,
  onCreateArchive,
  onAuthorizeGroup,
  onEditGroupPolicy,
  onOpenConfirm,
  isActionPending,
}: Props) {
  const [detailGroupId, setDetailGroupId] = React.useState<number | null>(null);
  const detailGroup = groups.find((group) => group.id === detailGroupId) ?? null;

  function openDetail(group: Group) {
    setSelectedGroupId(group.id);
    setDetailGroupId(group.id);
  }

  return (
    <>
      <Card className="panel" title="群聊库" extra={<Typography.Text type="secondary">按可运营、仅归档和不可操作来管理群聊使用范围</Typography.Text>}>
        <List
          className="group-list"
          dataSource={groups}
          renderItem={(group) => (
            <List.Item
              className={`${selectedGroupId === group.id ? 'selected group-card' : 'group-card'} ${statusAccent(group.auth_status)}`}
              onClick={() => setSelectedGroupId(group.id)}
              actions={[
                <StatusBadge status={group.auth_status} label={operationLabel(group.auth_status)} />,
                <Button size="small" onClick={(event) => { event.stopPropagation(); openDetail(group); }}>详情</Button>,
              ]}
            >
              <List.Item.Meta title={group.title} description={`${group.member_count.toLocaleString()} 成员 / ${group.group_type}`} />
            </List.Item>
          )}
        />
      </Card>

      <Modal
        className="tg-modal large"
        title={detailGroup?.title ?? '群聊详情'}
        open={Boolean(detailGroup)}
        width={820}
        footer={null}
        destroyOnHidden
        centered
        onCancel={() => setDetailGroupId(null)}
      >
        {detailGroup && (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <Descriptions className="detail-list" column={2} size="small" items={[
            { key: 'auth', label: '使用范围', children: <StatusBadge status={detailGroup.auth_status} label={operationLabel(detailGroup.auth_status)} /> },
            { key: 'active_window', label: '活跃时间', children: detailGroup.active_window },
            { key: 'daily_limit', label: '每日上限', children: `${detailGroup.daily_limit} 条` },
            { key: 'validation', label: '自动校验', children: <StatusBadge status="已启用" label="规则内自动" /> },
            { key: 'can_send', label: '可发言', children: <StatusBadge status={detailGroup.can_send ? '可发言' : '不可发言'} /> },
            { key: 'listener', label: '监听续聊', children: <StatusBadge status={detailGroup.listener_enabled ? '已启用' : '未配置'} label={detailGroup.listener_enabled ? `${detailGroup.listener_account_ids.length} 个监听号` : '未启用'} /> },
            { key: 'auto_reply', label: '自动发送', children: detailGroup.listener_auto_reply_enabled ? '监听触发后自动排队' : '只采集上下文' },
          ]} />
          {detailGroup.listener_last_error && <Typography.Paragraph type="danger">{detailGroup.listener_last_error}</Typography.Paragraph>}
          <Space className="detail-actions" wrap>
            <Button type="primary" icon={<MessageSquareText size={18} />} onClick={() => onCreateTask(detailGroup.id)}>用此群创建任务</Button>
            <Button icon={<Archive size={18} />} onClick={() => onOpenConfirm({
              title: '创建群归档',
              message: `确认归档「${detailGroup.title}」的消息与成员清单？`,
              confirmLabel: '创建归档',
              onConfirm: onCreateArchive,
            })}>创建归档</Button>
            {['已授权运营', '只读归档', '禁止操作'].map((status) => (
              <Button key={status} danger={status === '禁止操作'} loading={isActionPending(`group:${detailGroup.id}:authorize:${status}`)} onClick={() => onOpenConfirm({
                title: '更新群使用范围',
                message: `确认将「${detailGroup.title}」设置为「${operationLabel(status)}」？`,
                confirmLabel: '确认更新',
                tone: status === '禁止操作' ? 'danger' : 'normal',
                onConfirm: () => onAuthorizeGroup(status),
              })}>{operationLabel(status)}</Button>
            ))}
          </Space>
          <Card className="sub-panel compact-panel" title="运营配置" extra={<Button size="small" onClick={onEditGroupPolicy}>编辑运营配置</Button>}>
            <div className="summary-grid">
              <Card className="summary-card" size="small"><span>冷却规则</span><strong>账号 {detailGroup.account_cooldown_seconds}s</strong><p>群 {detailGroup.group_cooldown_seconds}s</p></Card>
              <Card className="summary-card" size="small"><span>内容边界</span><strong>{detailGroup.banned_words || '未配置禁用词'}</strong><p>{detailGroup.link_whitelist || '未配置链接白名单'}</p></Card>
              <Card className="summary-card" size="small"><span>话题方向</span><strong>{detailGroup.topic_direction || '未设置'}</strong><p>规则内自动校验</p></Card>
              <Card className="summary-card" size="small"><span>监听配置</span><strong>{detailGroup.listener_enabled ? `${detailGroup.listener_interval_seconds}s 轮询` : '未启用'}</strong><p>上下文 {detailGroup.listener_context_limit} 条</p></Card>
            </div>
          </Card>
          </Space>
        )}
      </Modal>
    </>
  );
}
