import React from 'react';
import { Button, Card, Descriptions, Empty, List, Modal, Space, Tabs, Typography } from 'antd';
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
  isActionPending: (key: string) => boolean;
}

function GroupCoveragePanel({ selectedGroup, groupDetail, onOpenGroupDetail, isActionPending }: Pick<Props, 'selectedGroup' | 'groupDetail' | 'onOpenGroupDetail' | 'isActionPending'>) {
  const [detailOpen, setDetailOpen] = React.useState(false);
  if (!selectedGroup) return <Card className="panel"><Empty description="暂无群聊资产" /></Card>;
  const currentDetail = groupDetail?.group.id === selectedGroup.id ? groupDetail : null;
  const accounts = currentDetail?.accounts ?? [];
  return (
    <>
      <Card
        className="panel"
        title="账号覆盖"
        extra={<Button loading={isActionPending(`group:${selectedGroup.id}:detail`)} onClick={() => { setDetailOpen(true); void onOpenGroupDetail(selectedGroup); }}>查看账号覆盖</Button>}
      >
        <Typography.Text type="secondary">同步群后自动形成群资产，并展示账号覆盖状态。</Typography.Text>
        <div className="stats-grid compact-stats">
          <Card className="summary-card" size="small"><span>覆盖账号</span><strong>{accounts.length || '-'}</strong><p>{currentDetail ? '已读取详情' : '点击查看后读取'}</p></Card>
          <Card className="summary-card" size="small"><span>可发言</span><strong>{accounts.filter((account) => account.can_send).length || '-'}</strong><p>按账号权限统计</p></Card>
          <Card className="summary-card" size="small"><span>监听账号</span><strong>{currentDetail?.listener_accounts.length ?? selectedGroup.listener_account_ids.length}</strong><p>用于上下文采集</p></Card>
        </div>
      </Card>

      <Modal className="tg-modal large" title={`${selectedGroup.title} 账号覆盖`} open={detailOpen} width={920} footer={null} destroyOnHidden centered onCancel={() => setDetailOpen(false)}>
        <List
          className="mini-list"
          dataSource={accounts}
          locale={{ emptyText: currentDetail ? '暂无账号覆盖。' : '正在读取账号覆盖详情。' }}
          renderItem={(account) => (
            <List.Item>
              <List.Item.Meta
                title={<Space><Typography.Text strong>{account.display_name}</Typography.Text><StatusBadge status={account.status} /></Space>}
                description={`@${account.username ?? '未设置'} / ${account.permission_label} / ${account.can_send ? '可发言' : '不可发言'} / 最近发送 ${account.last_sent_at ?? '-'}`}
              />
            </List.Item>
          )}
        />
      </Modal>
    </>
  );
}

function OperationPolicyPanel({
  selectedGroup,
  onCreateCampaign,
  onCreateArchive,
  onEditGroupPolicy,
  onAuthorizeGroup,
  onOpenConfirm,
  isActionPending,
}: Pick<Props, 'selectedGroup' | 'onCreateCampaign' | 'onCreateArchive' | 'onEditGroupPolicy' | 'onAuthorizeGroup' | 'onOpenConfirm' | 'isActionPending'>) {
  const [detailOpen, setDetailOpen] = React.useState(false);
  if (!selectedGroup) return <Card className="panel"><Empty description="暂无群聊资产" /></Card>;
  return (
    <>
      <Card className="panel" title="运营策略" extra={<Space><Button onClick={() => setDetailOpen(true)}>查看详情</Button><Button type="primary" onClick={onEditGroupPolicy}>编辑策略</Button></Space>}>
        <div className="stats-grid compact-stats">
          <Card className="summary-card" size="small"><span>授权策略</span><strong>{selectedGroup.auth_status}</strong><p>{selectedGroup.can_send ? '可发言' : '不可发言'}</p></Card>
          <Card className="summary-card" size="small"><span>活跃时间</span><strong>{selectedGroup.active_window}</strong><p>每日 {selectedGroup.daily_limit} 条</p></Card>
          <Card className="summary-card" size="small"><span>话题方向</span><strong>{selectedGroup.topic_direction || '未设置'}</strong><p>{selectedGroup.require_review ? '需要人工审核' : '规则内自动'}</p></Card>
        </div>
      </Card>

      <Modal className="tg-modal medium" title={`${selectedGroup.title} 运营策略`} open={detailOpen} width={820} footer={null} destroyOnHidden centered onCancel={() => setDetailOpen(false)}>
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
          {['已授权运营', '只读归档', '禁止操作'].map((status) => (
            <Button key={status} danger={status === '禁止操作'} loading={isActionPending(`group:${selectedGroup.id}:authorize:${status}`)} onClick={() => onOpenConfirm({
              title: '更新群使用范围',
              message: `确认将「${selectedGroup.title}」设置为「${status}」？`,
              confirmLabel: '确认更新',
              tone: status === '禁止操作' ? 'danger' : 'normal',
              onConfirm: () => onAuthorizeGroup(status),
            })}>{status}</Button>
          ))}
        </Space>
      </Modal>
    </>
  );
}

function ListenerContextPanel({ selectedGroup, groupDetail, onOpenGroupDetail, isActionPending }: Pick<Props, 'selectedGroup' | 'groupDetail' | 'onOpenGroupDetail' | 'isActionPending'>) {
  const [detailOpen, setDetailOpen] = React.useState(false);
  if (!selectedGroup) return <Card className="panel"><Empty description="暂无群聊资产" /></Card>;
  const currentDetail = groupDetail?.group.id === selectedGroup.id ? groupDetail : null;
  return (
    <>
      <Card className="panel" title="监听上下文" extra={<Button loading={isActionPending(`group:${selectedGroup.id}:detail`)} onClick={() => { setDetailOpen(true); void onOpenGroupDetail(selectedGroup); }}>查看上下文</Button>}>
        <div className="stats-grid compact-stats">
          <Card className="summary-card" size="small"><span>监听状态</span><strong>{selectedGroup.listener_enabled ? '已启用' : '未启用'}</strong><p>{selectedGroup.listener_auto_reply_enabled ? '自动续聊' : '只采集上下文'}</p></Card>
          <Card className="summary-card" size="small"><span>轮询间隔</span><strong>{selectedGroup.listener_interval_seconds}s</strong><p>上下文 {selectedGroup.listener_context_limit} 条</p></Card>
          <Card className="summary-card" size="small"><span>已读上下文</span><strong>{currentDetail?.recent_context_messages.length ?? '-'}</strong><p>{currentDetail ? '已读取详情' : '点击查看后读取'}</p></Card>
        </div>
      </Card>

      <Modal className="tg-modal large" title={`${selectedGroup.title} 监听上下文`} open={detailOpen} width={920} footer={null} destroyOnHidden centered onCancel={() => setDetailOpen(false)}>
        <Descriptions className="detail-list" column={2} size="small" items={[
          { key: 'enabled', label: '监听', children: <StatusBadge status={selectedGroup.listener_enabled ? '已启用' : '未配置'} /> },
          { key: 'auto', label: '自动续聊', children: selectedGroup.listener_auto_reply_enabled ? '自动排队' : '只采集上下文' },
          { key: 'interval', label: '轮询间隔', children: `${selectedGroup.listener_interval_seconds}s` },
          { key: 'limit', label: '上下文条数', children: selectedGroup.listener_context_limit },
        ]} />
        <div className="detail-columns">
          <List
            className="mini-list"
            header="最近上下文"
            dataSource={currentDetail?.recent_context_messages ?? []}
            locale={{ emptyText: currentDetail ? '暂无监听上下文。' : '正在读取监听上下文。' }}
            renderItem={(message) => <List.Item><Typography.Text strong>{message.sender_name}：</Typography.Text>{message.content}</List.Item>}
          />
          <List
            className="mini-list"
            header="监听账号"
            dataSource={currentDetail?.listener_accounts ?? []}
            locale={{ emptyText: currentDetail ? '暂无监听账号。' : '正在读取监听账号。' }}
            renderItem={(account) => (
              <List.Item>
                <List.Item.Meta title={<Space>{account.display_name}<StatusBadge status={account.status} /></Space>} description={`@${account.username ?? '未设置'}`} />
              </List.Item>
            )}
          />
        </div>
      </Modal>
    </>
  );
}

function MessageMemberLibrary({ archiveDetail }: Pick<Props, 'archiveDetail'>) {
  const [detailOpen, setDetailOpen] = React.useState(false);
  return (
    <>
      <Card className="panel" title="消息/成员库" extra={<Button disabled={!archiveDetail} onClick={() => setDetailOpen(true)}>查看详情</Button>}>
        {!archiveDetail ? (
          <Empty description="请先在归档任务里查看一个归档详情" />
        ) : (
          <div className="stats-grid compact-stats">
            <Card className="summary-card" size="small"><span>归档</span><strong>{archiveDetail.archive.title}</strong><p>{archiveDetail.archive.status}</p></Card>
            <Card className="summary-card" size="small"><span>消息样例</span><strong>{archiveDetail.messages.length}</strong><p>可打开详情查看</p></Card>
            <Card className="summary-card" size="small"><span>可邀请候选</span><strong>{archiveDetail.invite_candidates.length}</strong><p>按活跃度筛选</p></Card>
          </div>
        )}
      </Card>

      <Modal className="tg-modal large" title={`${archiveDetail?.archive.title ?? '消息/成员库'} 详情`} open={detailOpen} width={920} footer={null} destroyOnHidden centered onCancel={() => setDetailOpen(false)}>
        {archiveDetail ? (
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
        ) : <Empty description="暂无归档详情" />}
      </Modal>
    </>
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
    isActionPending,
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
              isActionPending={isActionPending}
            />
          ),
        },
        { key: 'coverage', label: '账号覆盖', children: <GroupCoveragePanel selectedGroup={selectedGroup} groupDetail={groupDetail} onOpenGroupDetail={onOpenGroupDetail} isActionPending={isActionPending} /> },
        { key: 'policy', label: '运营策略', children: <OperationPolicyPanel selectedGroup={selectedGroup} onCreateCampaign={onCreateCampaign} onCreateArchive={onCreateArchive} onEditGroupPolicy={onEditGroupPolicy} onAuthorizeGroup={onAuthorizeGroup} onOpenConfirm={onOpenConfirm} isActionPending={isActionPending} /> },
        { key: 'listener', label: '监听上下文', children: <ListenerContextPanel selectedGroup={selectedGroup} groupDetail={groupDetail} onOpenGroupDetail={onOpenGroupDetail} isActionPending={isActionPending} /> },
        { key: 'archives', label: '归档任务', children: <ArchivesView archives={archives} archiveDetail={archiveDetail} onOpenArchiveDetail={onOpenArchiveDetail} onExportArchive={onExportArchive} onRerunArchive={onRerunArchive} isActionPending={isActionPending} /> },
        { key: 'library', label: '消息/成员库', children: <MessageMemberLibrary archiveDetail={archiveDetail} /> },
      ]}
    />
  );
}
