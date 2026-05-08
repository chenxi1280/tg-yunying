import React from 'react';
import { Button, Card, Descriptions, Empty, List, Space, Tabs, Typography } from 'antd';
import type { ArchiveDetail, ArchiveItem, ConfirmPayload, Group, GroupDetail } from '../types';
import { StatusBadge } from '../components/shared';
import ArchivesView from './ArchivesView';
import GroupsView from './GroupsView';

interface Props {
  groups: Group[];
  selectedGroup: Group | undefined;
  selectedGroupId: number | null;
  groupDetail: GroupDetail | null;
  setSelectedGroupId: (id: number | null) => void;
  archives: ArchiveItem[];
  archiveDetail: ArchiveDetail | null;
  onCreateCampaign: (groupId?: number) => void;
  onCreateArchive: () => void;
  onAuthorizeGroup: (status: string) => void;
  onEditGroupPolicy: () => void;
  onOpenGroupDetail: (group: Group) => Promise<void>;
  onOpenArchiveDetail: (archive: ArchiveItem) => Promise<void>;
  onExportArchive: (archive: ArchiveItem) => Promise<void>;
  onRerunArchive: (archive: ArchiveItem) => Promise<void>;
  onOpenConfirm: (payload: ConfirmPayload) => void;
}

function GroupCoveragePanel({ selectedGroup, groupDetail, onOpenGroupDetail }: Pick<Props, 'selectedGroup' | 'groupDetail' | 'onOpenGroupDetail'>) {
  if (!selectedGroup) return <Card className="panel"><Empty description="暂无群聊资产" /></Card>;
  const currentDetail = groupDetail?.group.id === selectedGroup.id ? groupDetail : null;
  return (
    <Card
      className="panel"
      title="账号覆盖"
      extra={<Button onClick={() => void onOpenGroupDetail(selectedGroup)}>刷新账号覆盖</Button>}
    >
      <Typography.Text type="secondary">同步群后自动形成群资产，并展示哪些账号在群内、是否可发言和最近状态。</Typography.Text>
      <List
        className="mini-list"
        dataSource={currentDetail?.accounts ?? []}
        locale={{ emptyText: '点击刷新账号覆盖后查看账号在群内的状态。' }}
        renderItem={(account) => (
          <List.Item>
            <List.Item.Meta
              title={<Space><Typography.Text strong>{account.display_name}</Typography.Text><StatusBadge status={account.status} /></Space>}
              description={`@${account.username ?? '未设置'} / ${account.permission_label} / ${account.can_send ? '可发言' : '不可发言'} / 最近发送 ${account.last_sent_at ?? '-'}`}
            />
          </List.Item>
        )}
      />
    </Card>
  );
}

function OperationPolicyPanel({
  selectedGroup,
  onCreateCampaign,
  onCreateArchive,
  onEditGroupPolicy,
  onOpenConfirm,
}: Pick<Props, 'selectedGroup' | 'onCreateCampaign' | 'onCreateArchive' | 'onEditGroupPolicy' | 'onOpenConfirm'>) {
  if (!selectedGroup) return <Card className="panel"><Empty description="暂无群聊资产" /></Card>;
  return (
    <Card className="panel" title="运营策略" extra={<Button type="primary" onClick={onEditGroupPolicy}>编辑策略</Button>}>
      <Descriptions className="detail-list" column={2} size="small" items={[
        { key: 'auth', label: '授权策略', children: <StatusBadge status={selectedGroup.auth_status} /> },
        { key: 'can_send', label: '可发言', children: <StatusBadge status={selectedGroup.can_send ? '可发言' : '不可发言'} /> },
        { key: 'active_window', label: '活跃时间', children: selectedGroup.active_window },
        { key: 'daily_limit', label: '每日上限', children: `${selectedGroup.daily_limit} 条` },
        { key: 'topic', label: '话题方向', children: selectedGroup.topic_direction || '未设置' },
        { key: 'review', label: '审核', children: selectedGroup.require_review ? '需要人工审核' : '规则内自动' },
      ]} />
      <Space className="detail-actions" wrap>
        <Button type="primary" onClick={() => onCreateCampaign(selectedGroup.id)}>用此群创建任务</Button>
        <Button onClick={() => onOpenConfirm({
          title: '创建群归档',
          message: `确认归档「${selectedGroup.title}」的消息与成员清单？`,
          confirmLabel: '创建归档',
          onConfirm: onCreateArchive,
        })}>创建归档</Button>
      </Space>
    </Card>
  );
}

function ListenerContextPanel({ selectedGroup, groupDetail, onOpenGroupDetail }: Pick<Props, 'selectedGroup' | 'groupDetail' | 'onOpenGroupDetail'>) {
  if (!selectedGroup) return <Card className="panel"><Empty description="暂无群聊资产" /></Card>;
  const currentDetail = groupDetail?.group.id === selectedGroup.id ? groupDetail : null;
  return (
    <section className="view-grid">
      <Card className="panel" title="监听上下文" extra={<Button onClick={() => void onOpenGroupDetail(selectedGroup)}>刷新上下文</Button>}>
        <Descriptions className="detail-list" column={2} size="small" items={[
          { key: 'enabled', label: '监听', children: <StatusBadge status={selectedGroup.listener_enabled ? '已启用' : '未配置'} /> },
          { key: 'auto', label: '自动续聊', children: selectedGroup.listener_auto_reply_enabled ? '自动排队' : '只采集上下文' },
          { key: 'interval', label: '轮询间隔', children: `${selectedGroup.listener_interval_seconds}s` },
          { key: 'limit', label: '上下文条数', children: selectedGroup.listener_context_limit },
        ]} />
        <List
          className="mini-list"
          header="最近上下文"
          dataSource={currentDetail?.recent_context_messages ?? []}
          locale={{ emptyText: '暂无监听上下文。' }}
          renderItem={(message) => <List.Item><Typography.Text strong>{message.sender_name}：</Typography.Text>{message.content}</List.Item>}
        />
      </Card>
      <Card className="panel" title="监听账号">
        <List
          className="mini-list"
          dataSource={currentDetail?.listener_accounts ?? []}
          locale={{ emptyText: '暂无监听账号。' }}
          renderItem={(account) => (
            <List.Item>
              <List.Item.Meta title={<Space>{account.display_name}<StatusBadge status={account.status} /></Space>} description={`@${account.username ?? '未设置'}`} />
            </List.Item>
          )}
        />
      </Card>
    </section>
  );
}

function MessageMemberLibrary({ archiveDetail }: Pick<Props, 'archiveDetail'>) {
  return (
    <Card className="panel" title="消息/成员库" extra={<Typography.Text type="secondary">查看归档详情后可检索样例与邀请候选</Typography.Text>}>
      {!archiveDetail ? (
        <Empty description="请先在归档任务里查看一个归档详情" />
      ) : (
        <div className="detail-columns">
          <List
            header={`${archiveDetail.archive.title} / 消息`}
            dataSource={archiveDetail.messages}
            renderItem={(message) => <List.Item><Typography.Text strong>{message.sender_name}：</Typography.Text>{message.content}</List.Item>}
          />
          <List
            header="可邀请候选"
            dataSource={archiveDetail.invite_candidates}
            renderItem={(member) => <List.Item><Typography.Text strong>{member.display_name}</Typography.Text> @{member.username ?? '未设置'} / {member.tags} / {member.activity_score}</List.Item>}
          />
        </div>
      )}
    </Card>
  );
}

export default function GroupManagementView(props: Props) {
  const {
    groups,
    selectedGroup,
    selectedGroupId,
    groupDetail,
    setSelectedGroupId,
    archives,
    archiveDetail,
    onCreateCampaign,
    onCreateArchive,
    onAuthorizeGroup,
    onEditGroupPolicy,
    onOpenGroupDetail,
    onOpenArchiveDetail,
    onExportArchive,
    onRerunArchive,
    onOpenConfirm,
  } = props;

  return (
    <Tabs
      className="group-tabs"
      defaultActiveKey="assets"
      items={[
        {
          key: 'assets',
          label: '群聊资产',
          children: (
            <GroupsView
              groups={groups}
              selectedGroup={selectedGroup}
              selectedGroupId={selectedGroupId}
              setSelectedGroupId={setSelectedGroupId}
              onCreateCampaign={onCreateCampaign}
              onCreateArchive={onCreateArchive}
              onAuthorizeGroup={onAuthorizeGroup}
              onEditGroupPolicy={onEditGroupPolicy}
              onOpenConfirm={onOpenConfirm}
            />
          ),
        },
        { key: 'coverage', label: '账号覆盖', children: <GroupCoveragePanel selectedGroup={selectedGroup} groupDetail={groupDetail} onOpenGroupDetail={onOpenGroupDetail} /> },
        { key: 'policy', label: '运营策略', children: <OperationPolicyPanel selectedGroup={selectedGroup} onCreateCampaign={onCreateCampaign} onCreateArchive={onCreateArchive} onEditGroupPolicy={onEditGroupPolicy} onOpenConfirm={onOpenConfirm} /> },
        { key: 'listener', label: '监听上下文', children: <ListenerContextPanel selectedGroup={selectedGroup} groupDetail={groupDetail} onOpenGroupDetail={onOpenGroupDetail} /> },
        { key: 'archives', label: '归档任务', children: <ArchivesView archives={archives} archiveDetail={archiveDetail} onOpenArchiveDetail={onOpenArchiveDetail} onExportArchive={onExportArchive} onRerunArchive={onRerunArchive} /> },
        { key: 'library', label: '消息/成员库', children: <MessageMemberLibrary archiveDetail={archiveDetail} /> },
      ]}
    />
  );
}
