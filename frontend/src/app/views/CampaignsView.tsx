import React from 'react';
import { Activity, Bot, CheckCircle2, ClipboardCheck, Send } from 'lucide-react';
import { Button, Card, Empty, List, Select, Segmented, Space, Table, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { Campaign, Draft, MessageTask, Group, Account } from '../types';
import { StatCard, StatusBadge, Badge } from '../components/shared';
import { statusAccent, riskTone } from '../utils';

interface TaskSummary {
  campaigns: number;
  pendingDrafts: number;
  queued: number;
  sent: number;
  failed: number;
}

interface Props {
  campaigns: Campaign[];
  tasks: MessageTask[];
  drafts: Draft[];
  groups: Group[];
  accounts: Account[];
  taskManagementTab: string;
  setTaskManagementTab: (tab: string) => void;
  taskSummary: TaskSummary;
  selectedCampaign: Campaign | undefined;
  selectedCampaignDrafts: Draft[];
  selectedCampaignTasks: MessageTask[];
  taskStatusFilter: string;
  setTaskStatusFilter: (filter: string) => void;
  setSelectedCampaignId: (id: number | null) => void;
  onCreateCampaign: () => void;
  onCancelCampaign: (campaign: Campaign) => void;
  onApproveDraft: (draft: Draft) => void;
  onApproveAllDrafts: () => void;
  onDispatchTask: (task: MessageTask) => void;
  onRetryTask: (task: MessageTask) => void;
  onDrainQueue: () => void;
  onOpenConfirm: (payload: {
    title: string;
    message: string;
    confirmLabel: string;
    tone?: 'normal' | 'danger';
    onConfirm: () => void | Promise<void>;
  }) => void;
  groupName: (groupId: number | null | undefined) => string;
  accountName: (accountId: number | null | undefined) => string;
  isActionPending: (key: string) => boolean;
}

export default function CampaignsView({
  campaigns,
  tasks,
  drafts,
  groups,
  accounts,
  taskManagementTab,
  setTaskManagementTab,
  taskSummary,
  selectedCampaign,
  selectedCampaignDrafts,
  selectedCampaignTasks,
  taskStatusFilter,
  setTaskStatusFilter,
  setSelectedCampaignId,
  onCreateCampaign,
  onCancelCampaign,
  onApproveDraft,
  onApproveAllDrafts,
  onDispatchTask,
  onRetryTask,
  onDrainQueue,
  onOpenConfirm,
  groupName,
  accountName,
  isActionPending,
}: Props) {
  const taskColumns: ColumnsType<MessageTask> = [
    {
      title: '发送明细',
      key: 'detail',
      width: 420,
      render: (_, task) => (
        <Space direction="vertical" size={2}>
          <Typography.Text strong>发送明细 #{task.id}</Typography.Text>
          <Typography.Text>{task.content}</Typography.Text>
          <Typography.Text type="secondary">{task.target_type === 'private' ? `私发 ${task.target_display}` : groupName(task.group_id)} / 计划 {new Date(task.scheduled_at).toLocaleString()} / 延迟 {task.planned_delay_seconds}s / {task.message_type}{task.material_id ? ` #${task.material_id}` : ''}</Typography.Text>
          <Typography.Text type="secondary">计划账号：{accountName(task.preferred_account_id)} / 实际账号：{accountName(task.account_id)}{task.actual_account_changed ? ' / 已改派' : ''}</Typography.Text>
        </Space>
      ),
    },
    { title: '状态', key: 'status', width: 120, render: (_, task) => <StatusBadge status={task.status} /> },
    { title: '草稿', key: 'draft', width: 120, render: (_, task) => task.draft_id ? `草稿 #${task.draft_id}` : '无草稿' },
    { title: '失败类型', key: 'failure', width: 130, render: (_, task) => <StatusBadge status={task.failure_type ?? '无失败'} /> },
    {
      title: '操作',
      key: 'actions',
      width: 220,
      fixed: 'right',
      render: (_, task) => (
        <Space wrap>
          <Button type="primary" size="small" loading={isActionPending(`task:${task.id}:dispatch`)} disabled={task.status === '已发送'} onClick={() => onOpenConfirm({
            title: '触发消息调度',
            message: `确认触发发送明细 #${task.id} 的调度发送？`,
            confirmLabel: '触发调度',
            onConfirm: () => onDispatchTask(task),
          })}>触发调度</Button>
          <Button size="small" loading={isActionPending(`task:${task.id}:retry`)} disabled={task.status !== '失败'} onClick={() => onOpenConfirm({
            title: '重试失败任务',
            message: `确认重试发送明细 #${task.id}？`,
            confirmLabel: '重试',
            onConfirm: () => onRetryTask(task),
          })}>重试</Button>
        </Space>
      ),
    },
  ];

  return (
    <Card className="panel" title="任务管理" extra={<Button type="primary" icon={<Bot size={18} />} onClick={onCreateCampaign}>创建任务</Button>}>
      <Typography.Text type="secondary">统一查看运营任务、草稿审核和发送进度</Typography.Text>
      <div className="stats-grid compact-stats">
        <StatCard label="运营任务" value={taskSummary.campaigns} detail="任务总数" icon={<Activity size={22} />} />
        <StatCard label="待审核" value={taskSummary.pendingDrafts} detail="AI 草稿" icon={<ClipboardCheck size={22} />} />
        <StatCard label="排队中" value={taskSummary.queued} detail="消息任务" icon={<Send size={22} />} />
        <StatCard label="已发送" value={taskSummary.sent} detail={`失败 ${taskSummary.failed}`} icon={<CheckCircle2 size={22} />} />
      </div>
      <Segmented className="tabs-row" value={taskManagementTab} options={['任务列表', '草稿审核', '发送进度']} onChange={(value) => setTaskManagementTab(String(value))} />

      {taskManagementTab === '任务列表' && (
        <div className="cards-grid">
          {!campaigns.length && <Empty description="暂无运营任务" />}
          {campaigns.map((campaign) => {
            const campaignDrafts = drafts.filter((draft) => draft.campaign_id === campaign.id);
            const campaignTasks = tasks.filter((task) => task.campaign_id === campaign.id);
            const targetCount = campaign.target_group_ids ? campaign.target_group_ids.split(',').filter(Boolean).length : 1;
            const sourceCount = campaign.source_group_ids ? campaign.source_group_ids.split(',').filter(Boolean).length : 0;
            const modeLabel = campaign.execution_mode === 'mirror_forward' ? '监听转发' : campaign.execution_mode === 'ai_activity' ? 'AI 活跃' : '一次性草稿';
            return (
              <Card className={`task-card selectable-card ${selectedCampaign?.id === campaign.id ? 'selected' : ''} ${statusAccent(campaign.status)}`} key={campaign.id} size="small" onClick={() => setSelectedCampaignId(campaign.id)}>
                <StatusBadge status={campaign.status} />
                <Typography.Title level={3}>{campaign.title}</Typography.Title>
                <Typography.Paragraph>{campaign.topic}</Typography.Paragraph>
                <Typography.Text type="secondary">{modeLabel} / {campaign.intensity} / {campaign.send_window}</Typography.Text>
                <Typography.Text type="secondary">目标群 {targetCount} 个{sourceCount ? ` / 源群 ${sourceCount} 个` : ''} / 草稿 {campaignDrafts.length} 条 / 发送明细 {campaignTasks.length} 条</Typography.Text>
                {campaign.execution_mode !== 'manual_draft' && <Typography.Text type="secondary">结束 {campaign.ends_at ? new Date(campaign.ends_at).toLocaleString() : '未设置'} / Token {campaign.used_ai_tokens}{campaign.max_ai_tokens ? `/${campaign.max_ai_tokens}` : ''} / 过滤 {campaign.filtered_count}</Typography.Text>}
                {campaign.last_error && <Typography.Text type="danger">{campaign.last_error}</Typography.Text>}
                <Space><StatusBadge status="已发送" label={`已发送 ${campaignTasks.filter((task) => task.status === '已发送').length}`} /> <StatusBadge status={campaignTasks.some((task) => task.status === '失败') ? '失败' : '无失败'} label={`失败 ${campaignTasks.filter((task) => task.status === '失败').length}`} /></Space>
                {campaign.execution_mode !== 'manual_draft' && !['已完成', '已取消'].includes(campaign.status) && (
                  <Button size="small" danger loading={isActionPending(`campaign:${campaign.id}:cancel`)} onClick={(event) => {
                    event.stopPropagation();
                    onOpenConfirm({
                      title: '取消持续任务',
                      message: `确认停止「${campaign.title}」后续运行？`,
                      confirmLabel: '取消任务',
                      tone: 'danger',
                      onConfirm: () => onCancelCampaign(campaign),
                    });
                  }}>停止运行</Button>
                )}
              </Card>
            );
          })}
        </div>
      )}

      {taskManagementTab === '草稿审核' && (
        <div>
          <Space className="toolbar-row" wrap>
            <Typography.Text>{selectedCampaign ? `当前任务：${selectedCampaign.title}` : '请先选择任务'}</Typography.Text>
            <Button type="primary" loading={isActionPending(`campaign:${selectedCampaign?.id ?? 'current'}:approve-all`)} disabled={!selectedCampaign || !selectedCampaignDrafts.some((draft) => draft.status === '待审核')} onClick={() => onOpenConfirm({
              title: '批量通过草稿',
              message: selectedCampaign ? `确认批量通过「${selectedCampaign.title}」的待审核草稿，并生成消息任务？` : '请先选择任务',
              confirmLabel: '批量通过',
              onConfirm: onApproveAllDrafts,
            })} icon={<ClipboardCheck size={18} />}>批量通过当前任务草稿</Button>
          </Space>
          <List
            className="draft-list"
            dataSource={selectedCampaignDrafts}
            locale={{ emptyText: '当前任务还没有草稿，创建任务后系统会先生成待审核草稿。' }}
            renderItem={(draft) => (
              <List.Item className={`draft-card ${statusAccent(draft.status)}`} actions={[
                <Button type="primary" size="small" loading={isActionPending(`draft:${draft.id}:approve`)} disabled={draft.status === '已审核'} onClick={() => onOpenConfirm({
                  title: '审核通过草稿',
                  message: `确认通过这条「${draft.persona}」草稿并生成消息任务？`,
                  confirmLabel: '审核通过',
                  onConfirm: () => onApproveDraft(draft),
                })}>审核通过</Button>,
              ]}>
                <List.Item.Meta
                  title={<Space wrap><StatusBadge status={draft.status} /><Badge tone="neutral">{draft.persona}</Badge><Badge tone={riskTone(draft.risk_level)}>风险{draft.risk_level}</Badge><Badge tone="neutral">{draft.generation_source}</Badge><Badge tone="neutral">第 {draft.sequence_index || '-'} 轮</Badge></Space>}
                  description={<Space direction="vertical" size={2}><Typography.Text>{draft.content}</Typography.Text><Typography.Text type="secondary">建议发言：{accountName(draft.suggested_account_id)}{draft.reply_to_draft_id ? ` / 接草稿 #${draft.reply_to_draft_id}` : ''}</Typography.Text><Typography.Text type="secondary">{draft.provider_name} / {draft.model_name} / {draft.prompt_template_name}{draft.material_id ? ` / 素材 #${draft.material_id}` : ''}</Typography.Text>{draft.generation_error && <Typography.Text type="danger">{draft.generation_error}</Typography.Text>}</Space>}
                />
              </List.Item>
            )}
          />
        </div>
      )}

      {taskManagementTab === '发送进度' && (
        <div>
          <Space className="toolbar-row" wrap>
            <Select
              value={taskStatusFilter}
              onChange={(value) => setTaskStatusFilter(value)}
              style={{ width: 140 }}
              options={[
                { value: '', label: '全部状态' },
                { value: '排队中', label: '排队中' },
                { value: '发送中', label: '发送中' },
                { value: '已发送', label: '已发送' },
                { value: '失败', label: '失败' },
              ]}
            />
            <Typography.Text>{selectedCampaign ? `当前任务：${selectedCampaign.title}` : '请先选择任务'}</Typography.Text>
            <Button type="primary" loading={isActionPending('worker:drain')} disabled={!selectedCampaign} onClick={() => onOpenConfirm({
              title: '处理到期发送',
              message: '确认处理当前已经到时间的发送任务？',
              confirmLabel: '开始处理',
              onConfirm: onDrainQueue,
            })}>处理到期发送</Button>
          </Space>
          <Table<MessageTask>
            className="tg-table"
            rowKey="id"
            columns={taskColumns}
            dataSource={selectedCampaignTasks}
            pagination={false}
            scroll={{ x: 1010 }}
            locale={{ emptyText: '当前任务还没有发送明细，审核草稿后会生成待发送记录。' }}
          />
        </div>
      )}
    </Card>
  );
}
