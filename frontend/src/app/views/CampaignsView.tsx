import React from 'react';
import { Activity, Bot, CheckCircle2, ClipboardCheck, Send } from 'lucide-react';
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
  onApproveDraft: (draft: Draft) => void;
  onApproveAllDrafts: () => void;
  onDispatchTask: (task: MessageTask) => void;
  onRetryTask: (task: MessageTask) => void;
  onDrainQueue: () => void;
  onOpenConfirm: (payload: {
    title: string;
    message: string;
    confirmLabel: string;
    onConfirm: () => void | Promise<void>;
  }) => void;
  groupName: (groupId: number | null | undefined) => string;
  accountName: (accountId: number | null | undefined) => string;
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
  onApproveDraft,
  onApproveAllDrafts,
  onDispatchTask,
  onRetryTask,
  onDrainQueue,
  onOpenConfirm,
  groupName,
  accountName,
}: Props) {
  return (
    <section className="panel">
      <div className="section-title">
        <div>
          <h2>任务管理</h2>
          <span>统一查看运营任务、草稿审核和发送进度</span>
        </div>
        <button className="primary" onClick={onCreateCampaign}><Bot size={18} />创建任务</button>
      </div>
      <div className="stats-grid compact-stats">
        <StatCard label="运营任务" value={taskSummary.campaigns} detail="任务总数" icon={<Activity size={22} />} />
        <StatCard label="待审核" value={taskSummary.pendingDrafts} detail="AI 草稿" icon={<ClipboardCheck size={22} />} />
        <StatCard label="排队中" value={taskSummary.queued} detail="消息任务" icon={<Send size={22} />} />
        <StatCard label="已发送" value={taskSummary.sent} detail={`失败 ${taskSummary.failed}`} icon={<CheckCircle2 size={22} />} />
      </div>
      <div className="tabs-row">
        {['任务列表', '草稿审核', '发送进度'].map((tabName) => (
          <button key={tabName} className={taskManagementTab === tabName ? 'active' : ''} onClick={() => setTaskManagementTab(tabName)}>{tabName}</button>
        ))}
      </div>

      {taskManagementTab === '任务列表' && (
        <div className="cards-grid">
          {campaigns.map((campaign) => {
            const campaignDrafts = drafts.filter((draft) => draft.campaign_id === campaign.id);
            const campaignTasks = tasks.filter((task) => task.campaign_id === campaign.id);
            const targetCount = campaign.target_group_ids ? campaign.target_group_ids.split(',').filter(Boolean).length : 1;
            return (
              <article className={`task-card selectable-card ${selectedCampaign?.id === campaign.id ? 'selected' : ''} ${statusAccent(campaign.status)}`} key={campaign.id} onClick={() => setSelectedCampaignId(campaign.id)}>
                <StatusBadge status={campaign.status} />
                <h3>{campaign.title}</h3>
                <p>{campaign.topic}</p>
                <span>{campaign.campaign_type} / {campaign.intensity} / {campaign.send_window}</span>
                <span>目标群 {targetCount} 个 / 草稿 {campaignDrafts.length} 条 / 发送明细 {campaignTasks.length} 条</span>
                <span className="inline-status"><StatusBadge status="已发送" label={`已发送 ${campaignTasks.filter((task) => task.status === '已发送').length}`} /> <StatusBadge status={campaignTasks.some((task) => task.status === '失败') ? '失败' : '无失败'} label={`失败 ${campaignTasks.filter((task) => task.status === '失败').length}`} /></span>
              </article>
            );
          })}
        </div>
      )}

      {taskManagementTab === '草稿审核' && (
        <div>
          <div className="toolbar-row">
            <span>{selectedCampaign ? `当前任务：${selectedCampaign.title}` : '请先选择任务'}</span>
            <button className="primary" disabled={!selectedCampaign || !selectedCampaignDrafts.some((draft) => draft.status === '待审核')} onClick={() => onOpenConfirm({
              title: '批量通过草稿',
              message: selectedCampaign ? `确认批量通过「${selectedCampaign.title}」的待审核草稿，并生成消息任务？` : '请先选择任务',
              confirmLabel: '批量通过',
              onConfirm: onApproveAllDrafts,
            })}><ClipboardCheck size={18} />批量通过当前任务草稿</button>
          </div>
          <div className="draft-list">
            {selectedCampaignDrafts.map((draft) => (
              <article key={draft.id} className={`draft-card ${statusAccent(draft.status)}`}>
                <div>
                  <StatusBadge status={draft.status} />
                  <Badge tone="neutral">{draft.persona}</Badge>
                  <Badge tone={riskTone(draft.risk_level)}>风险{draft.risk_level}</Badge>
                  <Badge tone="neutral">{draft.generation_source}</Badge>
                  <Badge tone="neutral">第 {draft.sequence_index || '-'} 轮</Badge>
                </div>
                <p>{draft.content}</p>
                <span className="muted-line">
                  建议发言：{accountName(draft.suggested_account_id)}
                  {draft.reply_to_draft_id ? ` / 接草稿 #${draft.reply_to_draft_id}` : ''}
                </span>
                <span className="muted-line">{draft.provider_name} / {draft.model_name} / {draft.prompt_template_name}{draft.material_id ? ` / 素材 #${draft.material_id}` : ''}</span>
                {draft.generation_error && <span className="danger-text">{draft.generation_error}</span>}
                <button className="primary small" disabled={draft.status === '已审核'} onClick={() => onOpenConfirm({
                  title: '审核通过草稿',
                  message: `确认通过这条「${draft.persona}」草稿并生成消息任务？`,
                  confirmLabel: '审核通过',
                  onConfirm: () => onApproveDraft(draft),
                })}>审核通过</button>
              </article>
            ))}
            {!selectedCampaignDrafts.length && <p className="muted-line">当前任务还没有草稿，创建任务后系统会先生成待审核草稿。</p>}
          </div>
        </div>
      )}

      {taskManagementTab === '发送进度' && (
        <div>
          <div className="toolbar-row">
            <select value={taskStatusFilter} onChange={(event) => setTaskStatusFilter(event.target.value)}>
              <option value="">全部状态</option>
              <option value="排队中">排队中</option>
              <option value="发送中">发送中</option>
              <option value="已发送">已发送</option>
              <option value="失败">失败</option>
            </select>
            <span>{selectedCampaign ? `当前任务：${selectedCampaign.title}` : '请先选择任务'}</span>
            <button className="primary" disabled={!selectedCampaign} onClick={() => onOpenConfirm({
              title: '处理到期发送',
              message: '确认处理当前已经到时间的发送任务？',
              confirmLabel: '开始处理',
              onConfirm: onDrainQueue,
            })}>处理到期发送</button>
          </div>
          <div className="table">
            {selectedCampaignTasks.map((task) => (
              <div className={`table-row task-row ${statusAccent(task.status)}`} key={task.id}>
                <div>
                  <strong>发送明细 #{task.id}</strong>
                  <span>{task.content}</span>
                  <span>{task.target_type === 'private' ? `私发 ${task.target_display}` : groupName(task.group_id)} / 计划 {new Date(task.scheduled_at).toLocaleString()} / 延迟 {task.planned_delay_seconds}s / {task.message_type}{task.material_id ? ` #${task.material_id}` : ''}</span>
                  <span>计划账号：{accountName(task.preferred_account_id)} / 实际账号：{accountName(task.account_id)}{task.actual_account_changed ? ' / 已改派' : ''}</span>
                </div>
                <StatusBadge status={task.status} />
                <span>{task.draft_id ? `草稿 #${task.draft_id}` : '无草稿'}</span>
                <StatusBadge status={task.failure_type ?? '无失败'} />
                <div className="row-actions">
                  <button className="primary small" disabled={task.status === '已发送'} onClick={() => onOpenConfirm({
                    title: '触发消息调度',
                    message: `确认触发发送明细 #${task.id} 的调度发送？`,
                    confirmLabel: '触发调度',
                    onConfirm: () => onDispatchTask(task),
                  })}>触发调度</button>
                  <button className="small" disabled={task.status !== '失败'} onClick={() => onOpenConfirm({
                    title: '重试失败任务',
                    message: `确认重试发送明细 #${task.id}？`,
                    confirmLabel: '重试',
                    onConfirm: () => onRetryTask(task),
                  })}>重试</button>
                </div>
              </div>
            ))}
            {!selectedCampaignTasks.length && <p className="muted-line">当前任务还没有发送明细，审核草稿后会生成待发送记录。</p>}
          </div>
        </div>
      )}
    </section>
  );
}
