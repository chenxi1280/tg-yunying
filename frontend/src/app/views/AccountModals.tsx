import React from 'react';
import { Alert, Button, Card, Descriptions, Empty, Input, List, Modal, Select, Space, Table, Tabs, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type {
  Account, AccountPool, AccountDetail, AccountPoolDetail,
  AccountClonePlan, AccountCloneItem, VerificationTask, Contact,
  RuntimeConfig, CurrentUser, AccountGroup, MessageTask,
} from '../types';
import { FormActions, StatusBadge, useAntdTableControls } from '../components/shared';
import { statusAccent, operationLabel, syncTypeLabel } from '../utils';
import { api } from '../../shared/api/client';
import { formatBeijingDateTime, parseBeijingDate } from '../time';

const accountPhone = (account: Account) => account.phone_number || account.phone_masked;
const verificationTargetLabel = (task: VerificationTask) => task.target_display || task.target_peer_id || (task.group_id ? `群聊 #${task.group_id}` : '未识别目标');
const verificationActionLabel = (task: VerificationTask) => task.resolution_entry_label || (task.issue_category === 'group_restriction' ? '解除群限制' : '处理');
const verificationActionable = (task: VerificationTask) => ['待处理', '失败', '需人工处理'].includes(task.status);

// ===== Account Pool Detail Modal =====

interface AccountPoolDetailModalProps {
  accountPoolDetail: AccountPoolDetail;
  poolDirectAccountId: number | '';
  setPoolDirectAccountId: (id: number | '') => void;
  directMessageForm: { target_peer_id: string; target_display: string; content: string };
  setDirectMessageForm: (form: { target_peer_id: string; target_display: string; content: string }) => void;
  selectedDirectContact: Contact | null;
  onClose: () => void;
  onOpenAccountCreate: (loginNow: boolean) => void;
  onOpenAccountDetail: (account: Account) => Promise<void>;
  onRefreshAccountPoolDetail: () => Promise<void>;
  onStartDirectMessageToContact: (contact: Contact) => void;
  onCreateDirectMessageTask: () => Promise<void>;
  onOpenConfirm: (payload: {
    title: string;
    message: string;
    confirmLabel: string;
    restoreModalType?: 'accountDetail' | 'accountPoolDetail';
    onConfirm: () => Promise<void>;
  }) => void;
  onSetReturnAfterVerification: (mode: 'accountDetail' | 'accountPoolDetail') => void;
  onSetModal: (modal: any) => void;
  accountName: (accountId: number | null | undefined) => string;
  isActionPending: (key: string) => boolean;
  canCreateAccount?: boolean;
  canManualSend?: boolean;
}

export function AccountPoolDetailModal({
  accountPoolDetail, poolDirectAccountId, setPoolDirectAccountId,
  directMessageForm, setDirectMessageForm, selectedDirectContact,
  onClose, onOpenAccountCreate, onOpenAccountDetail,
  onRefreshAccountPoolDetail, onStartDirectMessageToContact,
  onCreateDirectMessageTask, onOpenConfirm, onSetReturnAfterVerification,
  onSetModal, accountName, isActionPending,
  canCreateAccount = true, canManualSend = true,
}: AccountPoolDetailModalProps) {
  return (
    <Modal className="tg-modal large" title={`${accountPoolDetail.pool.name} 账号分组`} open width={920} onCancel={onClose} footer={null} destroyOnHidden centered>
      <div className="modal-body">
      <Descriptions
        className="detail-list"
        size="small"
        column={2}
        items={[
          { key: 'accounts', label: '组内账号', children: `${accountPoolDetail.stats.accounts ?? 0} 个` },
          { key: 'online', label: '在线账号', children: `${accountPoolDetail.stats.online ?? 0} 个` },
          { key: 'contacts', label: '云联系人', children: `${accountPoolDetail.stats.contacts ?? 0} 个` },
          { key: 'verification', label: '待处理验证', children: <StatusBadge status={(accountPoolDetail.stats.verification_tasks ?? 0) ? '待处理' : '已完成'} label={accountPoolDetail.stats.verification_tasks ?? 0} /> },
        ]}
      />
      <div className="flow-sections">
        <Card className="sub-panel compact-panel" title="账号" extra={canCreateAccount ? <Button size="small" onClick={() => onOpenAccountCreate(true)}>新增账号</Button> : undefined}>
          <Typography.Text type="secondary">从池内账号进入详情、登录、同步和资料管理。</Typography.Text>
          <List
            className="mini-list"
            dataSource={accountPoolDetail.accounts}
            locale={{ emptyText: '这个账号分组还没有账号。' }}
            renderItem={(account) => (
              <List.Item className={statusAccent(account.status)} actions={[<Button size="small" loading={isActionPending(`account:${account.id}:detail`)} onClick={() => onOpenAccountDetail(account)}>进入账号</Button>]}>
                <List.Item.Meta
                  title={<Space><StatusBadge status={account.status} />{account.display_name}</Space>}
                  description={`${accountPhone(account)} / 健康分 ${Math.round(account.health_score)}`}
                />
              </List.Item>
            )}
          />
        </Card>

        <Card className="sub-panel compact-panel" title="云联系人发送" extra={<Button size="small" loading={isActionPending(`account-pool:${accountPoolDetail.pool.id}:refresh`)} onClick={onRefreshAccountPoolDetail}>刷新账号分组</Button>}>
          <Typography.Text type="secondary">先选择发送账号，再从已同步联系人或群友中选择对象。</Typography.Text>
          <div className="policy-grid">
            <label>发送账号<Select value={poolDirectAccountId || ''} onChange={(value) => setPoolDirectAccountId(Number(value) || '')} options={[{ value: '', label: '选择发送账号' }, ...accountPoolDetail.accounts.map((account) => ({ value: account.id, label: `${account.display_name} / ${account.status === '在线' ? '可发送' : account.status}`, disabled: account.status !== '在线' }))]} /></label>
          </div>
          <div className="contact-pick-grid">
            {accountPoolDetail.contacts.filter((contact) => !poolDirectAccountId || contact.account_id === poolDirectAccountId).map((contact) => (
              <Button key={contact.id} className={selectedDirectContact?.id === contact.id ? 'selected contact-pick' : 'contact-pick'} onClick={() => onStartDirectMessageToContact(contact)}>
                <strong>{contact.display_name}</strong>
                <span>{contact.username ? `@${contact.username}` : contact.peer_id}</span>
                <small>{accountName(contact.account_id)} / {contact.contact_type === 'group_member' ? '群友候选' : '私聊对象'}</small>
              </Button>
            ))}
            {!accountPoolDetail.contacts.filter((contact) => !poolDirectAccountId || contact.account_id === poolDirectAccountId).length && (
              <p className="muted-line">这个账号还没有同步到可私发对象，可以先进入账号执行同步。</p>
            )}
          </div>
          <div className="policy-grid">
            <div className="wide-field selected-recipient-box">
              <span>当前发送对象</span>
              <strong>{selectedDirectContact ? selectedDirectContact.display_name : '请选择联系人或群友'}</strong>
            </div>
            <label className="wide-field">消息内容<Input.TextArea value={directMessageForm.content} onChange={(event) => setDirectMessageForm({ ...directMessageForm, content: event.target.value })} /></label>
            <div className="wide-field detail-actions">
              {canManualSend && <Button type="primary" disabled={!poolDirectAccountId || !selectedDirectContact || !directMessageForm.content} onClick={() => onOpenConfirm({
                title: '创建池内私发任务',
                message: `确认用「${accountName(poolDirectAccountId || null)}」向「${selectedDirectContact?.display_name ?? ''}」发送这条消息？`,
                confirmLabel: '创建并发送',
                restoreModalType: 'accountPoolDetail',
                onConfirm: onCreateDirectMessageTask,
              })}>创建并发送</Button>}
            </div>
          </div>
        </Card>

        <Card className="sub-panel compact-panel" title="克隆和验证">
          <Typography.Text type="secondary">查看池内克隆计划与需要人工确认的验证事项。</Typography.Text>
          {!accountPoolDetail.clone_plans.length && !accountPoolDetail.verification_tasks.length && <Empty description="暂无克隆计划或验证事项" />}
          <List
            className="mini-list"
            dataSource={accountPoolDetail.clone_plans.slice(0, 4)}
            renderItem={(plan) => (
              <List.Item className={statusAccent(plan.status)}>
                <List.Item.Meta
                  title={<Space><StatusBadge status={plan.status} />克隆计划 #{plan.id}</Space>}
                  description={<Space direction="vertical" size={0}><span>{plan.target_accounts_summary.map((item) => item.display_name).join('、') || accountName(plan.target_account_id)}</span><span>总 {plan.items_total} / 完成 {plan.items_done} / 失败 {plan.items_failed}</span></Space>}
                />
              </List.Item>
            )}
          />
          <List
            className="mini-list"
            dataSource={accountPoolDetail.verification_tasks.slice(0, 4)}
            renderItem={(task) => (
              <List.Item className={statusAccent(task.status)} actions={[<Button size="small" onClick={() => { onSetReturnAfterVerification('accountPoolDetail'); onSetModal({ type: 'verificationTaskDetail', payload: task }); }}>{verificationActionLabel(task)}</Button>]}>
                <List.Item.Meta
                  title={<Space><StatusBadge status={task.status} />{task.verification_type}</Space>}
                  description={<Space direction="vertical" size={0}><span>目标：{verificationTargetLabel(task)}</span><span>{task.detected_reason || task.suggested_action}</span></Space>}
                />
              </List.Item>
            )}
          />
        </Card>
      </div>
      </div>
    </Modal>
  );
}

// ===== Account Detail Modal =====

interface AccountDetailModalProps {
  accountDetail: AccountDetail;
  accountDetailTab: string;
  setAccountDetailTab: (tab: string) => void;
  runtime: RuntimeConfig | null;
  directMessageForm: { target_peer_id: string; target_display: string; content: string };
  setDirectMessageForm: (form: { target_peer_id: string; target_display: string; content: string }) => void;
  selectedDirectContact: Contact | null;
  accountContacts: Contact[];
  accounts: Account[];
  avatarUrl: (value: string) => string;
  onClose: () => void;
  onOpenAccountProfileEdit: () => void;
  onQueueAccountSyncNow: () => Promise<void>;
  onRefreshAccountDetail: () => Promise<void>;
  onPollVerificationCodes: (silent?: boolean) => Promise<void>;
  onStartDirectMessageToContact: (contact: Contact) => void;
  onCreateDirectMessageTask: () => Promise<void>;
  onConfirmClonePlan: (plan: AccountClonePlan) => Promise<void>;
  onRetryCloneItem: (item: AccountCloneItem) => Promise<void>;
  onRetryAccountProfileSync: () => Promise<void>;
  onDismissVerificationTask: (task: VerificationTask) => Promise<void>;
  onConfirmVerificationTask: (task: VerificationTask) => Promise<void>;
  onResolveGroupRestrictionTask: (task: VerificationTask) => Promise<void>;
  onOpenConfirm: (payload: {
    title: string;
    message: string;
    confirmLabel: string;
    restoreModalType?: 'accountDetail' | 'accountPoolDetail';
    onConfirm: () => Promise<void>;
  }) => void;
  onSetReturnAfterVerification: (mode: 'accountDetail' | 'accountPoolDetail') => void;
  onSetModal: (modal: any) => void;
  onSetCloneForm: (form: { target_account_ids: number[]; clone_contacts: boolean; clone_groups: boolean }) => void;
  accountName: (accountId: number | null | undefined) => string;
  isActionPending: (key: string) => boolean;
  canUpdateProfile?: boolean;
  canSyncAccount?: boolean;
  canViewCodes?: boolean;
  canMovePool?: boolean;
  canClone?: boolean;
}

export function AccountDetailModal({
  accountDetail, accountDetailTab, setAccountDetailTab, runtime,
  directMessageForm, setDirectMessageForm, selectedDirectContact,
  accountContacts, accounts, avatarUrl, onClose,
  onOpenAccountProfileEdit, onQueueAccountSyncNow, onPollVerificationCodes,
  onRefreshAccountDetail,
  onStartDirectMessageToContact, onCreateDirectMessageTask,
  onConfirmClonePlan, onRetryCloneItem,
  onRetryAccountProfileSync,
  onDismissVerificationTask, onConfirmVerificationTask, onResolveGroupRestrictionTask,
  onOpenConfirm, onSetReturnAfterVerification, onSetModal,
  onSetCloneForm, accountName, isActionPending,
  canUpdateProfile = true, canSyncAccount = true, canViewCodes = true, canMovePool = true, canClone = true,
}: AccountDetailModalProps) {
  const [manualTargetId, setManualTargetId] = React.useState<number | null>(null);
  const [manualContent, setManualContent] = React.useState('');
  const [manualSending, setManualSending] = React.useState(false);

  React.useEffect(() => {
    setManualTargetId((current) => current ?? accountDetail.operation_targets[0]?.id ?? null);
  }, [accountDetail.operation_targets]);

  React.useEffect(() => {
    if (!canViewCodes) return undefined;
    if (accountDetailTab !== 'TG 官方验证码') return undefined;
    void onPollVerificationCodes(true);
    const timer = window.setInterval(() => {
      void onPollVerificationCodes(true);
    }, 10000);
    return () => window.clearInterval(timer);
  }, [accountDetail.account.id, accountDetailTab, canViewCodes]);

  async function syncTargets() {
    await api(`/tg-accounts/${accountDetail.account.id}/sync-targets`, { method: 'POST' });
    await onRefreshAccountDetail();
  }

  async function manualSendNow() {
    if (!manualTargetId || !manualContent.trim()) return;
    setManualSending(true);
    try {
      await api(`/tg-accounts/${accountDetail.account.id}/manual-send`, {
        method: 'POST',
        body: JSON.stringify({ target_id: manualTargetId, content: manualContent }),
      });
      setManualContent('');
      await onRefreshAccountDetail();
      setAccountDetailTab('执行记录');
    } finally {
      setManualSending(false);
    }
  }

  const groupColumns: ColumnsType<AccountGroup> = [
    {
      title: '群聊',
      key: 'group',
      render: (_, group) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{group.title}</Typography.Text>
          <Typography.Text type="secondary">{group.member_count.toLocaleString()} 成员 / {group.permission_label}</Typography.Text>
        </Space>
      ),
    },
    { title: '使用范围', key: 'auth_status', width: 140, render: (_, group) => <StatusBadge status={group.auth_status} label={operationLabel(group.auth_status)} /> },
    { title: '账号权限', key: 'account_can_send', width: 140, render: (_, group) => <StatusBadge status={group.account_can_send ? '账号可发言' : '账号不可发言'} /> },
    { title: '最近发送', key: 'last_sent_at', width: 200, render: (_, group) => group.last_sent_at ? formatBeijingDateTime(group.last_sent_at) : '暂无发送' },
  ];

  const messageColumns: ColumnsType<MessageTask> = [
    {
      title: '任务',
      key: 'task',
      render: (_, task) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>任务 #{task.id}</Typography.Text>
          <Typography.Text type="secondary">{task.target_type === 'private' ? `私发：${task.target_display}` : `群任务：${task.group_id}`}</Typography.Text>
          <Typography.Text>{task.content}</Typography.Text>
        </Space>
      ),
    },
    { title: '状态', key: 'status', width: 120, render: (_, task) => <StatusBadge status={task.status} /> },
    { title: '失败类型', key: 'failure', width: 130, render: (_, task) => <StatusBadge status={task.failure_type ?? '无失败'} /> },
    { title: '时间', key: 'time', width: 200, render: (_, task) => formatBeijingDateTime(task.sent_at ?? task.scheduled_at) },
  ];

  const groupTable = useAntdTableControls<AccountGroup>({
    rows: accountDetail.groups,
    pageSize: 5,
    pageSizeOptions: [5, 10, 20, 50],
    placeholder: '搜索群聊 / 权限 / 状态',
    search: [
      (group) => [
        group.id,
        group.title,
        group.member_count,
        group.permission_label,
        group.auth_status,
        operationLabel(group.auth_status),
        group.account_can_send ? '账号可发言' : '账号不可发言',
        group.last_sent_at,
      ],
    ],
  });

  const messageTable = useAntdTableControls<MessageTask>({
    rows: accountDetail.message_records,
    pageSize: 5,
    pageSizeOptions: [5, 10, 20, 50],
    placeholder: '搜索任务 / 目标 / 内容 / 状态',
    search: [
      (task) => [
        task.id,
        task.target_type,
        task.target_display,
        task.group_id,
        task.content,
        task.status,
        task.failure_type,
        task.scheduled_at,
        task.sent_at,
      ],
    ],
  });

  const riskLevelTone = accountDetail.risk_diagnostics[0]?.level === '高' ? 'error' : accountDetail.risk_diagnostics[0]?.level === '中' ? 'warning' : 'success';
  const riskSummary = accountDetail.risk_diagnostics[0]
    ? `${accountDetail.risk_diagnostics[0].level}风险 ${accountDetail.risk_diagnostics[0].title}，共 ${accountDetail.risk_diagnostics.length} 条`
    : '账号风险正常';
  const latestLoginAt = accountDetail.login_flows[0]?.created_at ?? null;
  const latestAnySync = accountDetail.sync_records.find((record) => record.finished_at)?.finished_at ?? accountDetail.sync_records[0]?.created_at ?? null;
  const latestProfilePull = accountDetail.account.profile_synced_at ?? accountDetail.sync_records.find((record) => record.sync_type === 'profile_pull' && record.finished_at)?.finished_at ?? null;
  const latestCodeSync = accountDetail.sync_records.find((record) => record.sync_type === 'codes' && record.finished_at)?.finished_at ?? accountDetail.verification_codes[0]?.created_at ?? null;
  const latestVisibleCode = accountDetail.verification_codes.find((code) => code.code_preview) ?? accountDetail.verification_codes[0] ?? null;
  const formatTime = (value: string | null | undefined) => value ? formatBeijingDateTime(value) : '暂无记录';
  const groupCooldowns = accountDetail.groups
    .map((group) => {
      if (!group.last_sent_at || !group.group_cooldown_seconds) return null;
      const cooldownUntil = new Date((parseBeijingDate(group.last_sent_at)?.getTime() ?? 0) + group.group_cooldown_seconds * 1000);
      return cooldownUntil.getTime() > Date.now() ? { group, cooldownUntil } : null;
    })
    .filter((item): item is { group: AccountGroup; cooldownUntil: Date } => Boolean(item));
  const accountRestrictedRisks = accountDetail.risk_diagnostics.filter((risk) => (
    risk.code === 'ACCOUNT_STATUS'
    || risk.code === 'SESSION_MISSING'
    || risk.title.includes('账号')
    || risk.title.includes('Session')
  ));
  const groupRestrictionTasks = accountDetail.verification_tasks.filter((task) => task.issue_category === 'group_restriction');
  const autoVerificationTasks = accountDetail.verification_tasks.filter((task) => task.issue_category !== 'group_restriction' && task.can_auto_resolve);
  const manualVerificationTasks = accountDetail.verification_tasks.filter((task) => task.issue_category !== 'group_restriction' && !task.can_auto_resolve && verificationActionable(task));

  return (
    <Modal className="tg-modal large" title={`${accountDetail.account.display_name} 账号详情`} open width={920} onCancel={onClose} footer={null} destroyOnHidden centered>
      <div className="modal-body">
      <div className="account-detail-summary">
        <div><span>账号状态</span><strong><StatusBadge status={accountDetail.account.status} /></strong></div>
        <div><span>手机号</span><strong>{accountPhone(accountDetail.account)}</strong></div>
        <div><span>所属账号分组</span><strong>{accountDetail.account.pool_name}</strong></div>
        <div><span>资料同步</span><strong><StatusBadge status={accountDetail.account.profile_sync_status} /></strong></div>
        <div><span>加入群聊</span><strong>{accountDetail.stats.joined_groups ?? 0} 个</strong></div>
        <div><span>发送记录</span><strong>{accountDetail.stats.message_records ?? 0} 条</strong></div>
        <div><span>待处理验证</span><strong><StatusBadge status={(accountDetail.stats.pending_verification_tasks ?? 0) ? '待处理' : '已完成'} label={accountDetail.stats.pending_verification_tasks ?? 0} /></strong></div>
        <div>
          <span>成功/失败</span>
          <strong><Space size={6}><StatusBadge status="已发送" label={accountDetail.stats.sent ?? 0} /><StatusBadge status={(accountDetail.stats.failed ?? 0) > 0 ? '失败' : '无失败'} label={accountDetail.stats.failed ?? 0} /></Space></strong>
        </div>
      </div>
      <Card className="sub-panel compact-panel account-risk-card" size="small">
        <Space direction="vertical" size={8}>
          <Alert
            type={riskLevelTone}
            showIcon
            message={riskSummary}
            description={accountDetail.risk_diagnostics[0]?.detail || '当前没有受限、封禁、FloodWait、目标不可访问或待处理验证信号。'}
          />
          <Space wrap>
            {canUpdateProfile && <Button type="primary" size="small" onClick={onOpenAccountProfileEdit}>编辑资料</Button>}
            {canMovePool && <Button size="small" onClick={() => onSetModal({ type: 'accountMovePool' })}>移动分组</Button>}
            {canSyncAccount && <Button size="small" loading={isActionPending(`account:${accountDetail.account.id}:sync`)} onClick={onQueueAccountSyncNow}>同步</Button>}
            {canViewCodes && <Button size="small" loading={isActionPending(`account:${accountDetail.account.id}:codes`)} onClick={() => { setAccountDetailTab('TG 官方验证码'); void onPollVerificationCodes(); }}>提取验证码</Button>}
          </Space>
        </Space>
      </Card>
      <Card className="sub-panel compact-panel" size="small">
        <div className="section-title">
          <div>
            <h2>处理分流</h2>
            <span>账号级问题、群内拦截和可自动处理验证分开处理，避免把群限制误当成账号重新登录。</span>
          </div>
          <Button size="small" loading={isActionPending(`account:${accountDetail.account.id}:detail-refresh`)} onClick={onRefreshAccountDetail}>刷新状态</Button>
        </div>
        <div className="summary-grid">
          <div className="summary-card">
            <span>账号级受限</span>
            <strong>{accountRestrictedRisks.length ? accountRestrictedRisks[0].title : '正常'}</strong>
            <p>{accountRestrictedRisks[0]?.action || '系统会每小时按健康检查和同步结果判断是否恢复。'}</p>
            <Space wrap size={6}>
              <Button size="small" onClick={() => setAccountDetailTab('账号状态记录')}>查看状态记录</Button>
              {canSyncAccount && <Button size="small" loading={isActionPending(`account:${accountDetail.account.id}:sync`)} onClick={onQueueAccountSyncNow}>同步探测</Button>}
            </Space>
          </div>
          <div className="summary-card">
            <span>群管理机器拦截</span>
            <strong>{groupRestrictionTasks.length} 个目标</strong>
            <p>{groupRestrictionTasks.length ? '管理员在群内解除后，回到这里重查目标能力。' : '暂无需要群管理员解除的目标限制。'}</p>
            <Space wrap size={6}>
              <Button size="small" disabled={!groupRestrictionTasks.length} onClick={() => setAccountDetailTab('验证待处理')}>查看群限制</Button>
              {groupRestrictionTasks.slice(0, 1).map((task) => (
                <Button key={task.id} size="small" type="primary" loading={isActionPending(`verification:${task.id}:resolve-group`)} disabled={!verificationActionable(task)} onClick={() => onResolveGroupRestrictionTask(task)}>重查最近目标</Button>
              ))}
            </Space>
          </div>
          <div className="summary-card">
            <span>可自动验证</span>
            <strong>{autoVerificationTasks.length} 项</strong>
            <p>{autoVerificationTasks.length ? '关注频道、点击按钮、发送验证回复会走验证辅助动作。' : '暂无可自动执行的验证动作。'}</p>
            <Button size="small" disabled={!autoVerificationTasks.length} onClick={() => setAccountDetailTab('验证待处理')}>处理验证</Button>
          </div>
          <div className="summary-card">
            <span>其他人工事项</span>
            <strong>{manualVerificationTasks.length} 项</strong>
            <p>{manualVerificationTasks.length ? '需要操作员确认完成，但不会直接放行群权限。' : '暂无其他人工验证事项。'}</p>
            <Button size="small" disabled={!manualVerificationTasks.length} onClick={() => setAccountDetailTab('验证待处理')}>查看事项</Button>
          </div>
        </div>
      </Card>
      <Tabs
        className="tabs-row"
        activeKey={accountDetailTab}
        onChange={setAccountDetailTab}
        items={['资料', '账号状态记录', ...(canViewCodes ? ['TG 官方验证码'] : []), '验证待处理', '执行记录', ...(canClone ? ['克隆'] : [])].map((tabName) => ({ key: tabName, label: tabName }))}
      />

      {accountDetailTab === '资料' && (
        <Card className="sub-panel compact-panel">
          <div className="section-title">
            <div>
              <h2>账号资料</h2>
              <span>平台备注名用于后台识别，TG 昵称、简介和头像会通过同步任务更新到真实账号。</span>
            </div>
            <div className="row-actions">
              {canUpdateProfile && <Button type="primary" size="small" onClick={onOpenAccountProfileEdit}>编辑资料</Button>}
              <Button size="small" loading={isActionPending(`account:${accountDetail.account.id}:profile-sync`)} disabled={accountDetail.account.profile_sync_status !== '失败'} onClick={() => onOpenConfirm({
                title: '重试资料同步',
                message: `确认重新同步「${accountDetail.account.display_name}」的 TG 资料？`,
                confirmLabel: '重新入队',
                restoreModalType: 'accountDetail',
                onConfirm: onRetryAccountProfileSync,
              })}>重试同步</Button>
            </div>
          </div>
          <div className="profile-layout">
            <div className="avatar-preview">
              {accountDetail.account.avatar_preview_url ? <img src={avatarUrl(accountDetail.account.avatar_preview_url)} alt="" /> : <span>{accountDetail.account.display_name.slice(0, 1)}</span>}
            </div>
            <div className="detail-list">
              <div><dt>平台备注名</dt><dd>{accountDetail.account.display_name}</dd></div>
              <div><dt>TG 昵称</dt><dd>{[accountDetail.account.tg_first_name, accountDetail.account.tg_last_name].filter(Boolean).join(' ') || '未设置'}</dd></div>
              <div><dt>TG 简介</dt><dd>{accountDetail.account.tg_bio || '未设置'}</dd></div>
              <div><dt>最近同步</dt><dd>{accountDetail.account.profile_synced_at ? formatBeijingDateTime(accountDetail.account.profile_synced_at) : '暂无成功同步'}</dd></div>
            </div>
          </div>
          {accountDetail.account.profile_sync_error && <p className="danger-text">{accountDetail.account.profile_sync_error}</p>}
          <div className="mini-list">
            {accountDetail.profile_sync_records.map((record) => (
              <Card key={record.id} size="small">
                <StatusBadge status={record.status} />
                <strong>同步记录 #{record.id}</strong>
                <span>{record.actor || '系统'} / {formatBeijingDateTime(record.created_at)}</span>
                <span>{record.remote_detail || record.failure_detail || '等待处理'}</span>
              </Card>
            ))}
            {!accountDetail.profile_sync_records.length && <p className="muted-line">暂无资料同步记录。</p>}
          </div>
        </Card>
      )}

      {accountDetailTab === '账号状态记录' && (
        <div className="flow-sections">
          <Card className="sub-panel compact-panel" title="风险与等待状态">
            <div className="mini-list">
              {accountDetail.risk_diagnostics.map((risk) => (
                <Card key={`${risk.code}-${risk.source}-${risk.occurred_at ?? risk.title}`} className={statusAccent(risk.level)} size="small">
                  <StatusBadge status={risk.level} label={risk.title} />
                  <strong>{risk.source}</strong>
                  <span>{risk.detail}</span>
                  <span>建议：{risk.action}</span>
                  <span>{formatTime(risk.occurred_at)}</span>
                </Card>
              ))}
              {groupCooldowns.map(({ group, cooldownUntil }) => (
                <Card key={`cooldown-${group.id}`} className={statusAccent('待冷却')} size="small">
                  <StatusBadge status="待冷却" label="群冷却中" />
                  <strong>{group.title}</strong>
                  <span>该目标处于发送冷却，需等待目标群限制解除后再恢复排布。</span>
                  <span>可恢复时间：{formatBeijingDateTime(cooldownUntil)}</span>
                </Card>
              ))}
              {!accountDetail.risk_diagnostics.length && !groupCooldowns.length && <Empty description="暂无高风险、受限、待验证或冷却状态" />}
            </div>
          </Card>
          <Card className="sub-panel compact-panel" title="同步与登录记录">
            <Descriptions
              className="detail-list"
              size="small"
              column={2}
              items={[
                { key: 'latest-sync', label: '最近同步', children: formatTime(latestAnySync) },
                { key: 'latest-login', label: '最近登录', children: formatTime(latestLoginAt) },
                { key: 'profile-pull', label: '最近资料拉取', children: formatTime(latestProfilePull) },
                { key: 'code-sync', label: '最近验证码同步', children: formatTime(latestCodeSync) },
                { key: 'next-sync', label: '预计下次同步', children: accountDetail.next_sync_at ? formatTime(accountDetail.next_sync_at) : accountDetail.sync_status_text || (accountDetail.sync_due ? '已到同步时间，等待后台执行' : '暂无计划') },
              ]}
            />
            <div className="mini-list">
              {accountDetail.sync_records.slice(0, 6).map((record) => (
                <Card key={`sync-${record.id}`} className={statusAccent(record.status)} size="small">
                  <StatusBadge status={record.status} label={syncTypeLabel(record.sync_type)} />
                  <strong>{record.status}</strong>
                  <span>{record.result_count ? `已同步 ${record.result_count} 条` : record.failure_detail || '等待后台处理'}</span>
                  <span>{formatTime(record.finished_at || record.started_at || record.created_at)}</span>
                </Card>
              ))}
              {!accountDetail.sync_records.length && <p className="muted-line">登录成功后会自动同步资料、健康、群聊、云联系人和 TG 官方验证码。</p>}
            </div>
          </Card>
        </div>
      )}

      {canViewCodes && accountDetailTab === 'TG 官方验证码' && (
        <div className="flow-sections">
          <Card className="sub-panel compact-panel" title="TG 官方验证码" extra={canViewCodes ? <Button size="small" type="primary" loading={isActionPending(`account:${accountDetail.account.id}:codes`)} onClick={() => onPollVerificationCodes()}>同步提取官方验证码</Button> : undefined}>
            {latestVisibleCode ? (
              <div className="verification-code-card">
                <StatusBadge status={latestVisibleCode.code_preview ? '可查看' : latestVisibleCode.status} label={latestVisibleCode.source === 'login_flow' ? '登录验证码' : 'TG 官方验证码'} />
                <strong>{latestVisibleCode.code_preview || latestVisibleCode.status || '暂无新验证码'}</strong>
                  <span>{latestVisibleCode.expires_at ? `有效到 ${formatBeijingDateTime(latestVisibleCode.expires_at)}` : '等待新的验证码'}</span>
                {runtime?.show_advanced_debug && <small>{latestVisibleCode.raw_hint || latestVisibleCode.source}</small>}
              </div>
            ) : (
              <Empty description="暂无 TG 官方验证码" />
            )}
            <div className="mini-list">
              {accountDetail.verification_codes.slice(0, 6).map((code) => (
                <Card key={code.id} size="small">
                  <StatusBadge status={code.code_preview ? '可查看' : code.status} label={code.source === 'login_flow' ? '登录验证码' : 'TG 官方验证码'} />
                  <strong>{code.code_preview ? `验证码 ${code.code_preview}` : code.status}</strong>
                  <span>{code.expires_at ? `有效到 ${formatBeijingDateTime(code.expires_at)}` : '等待新的验证码'}</span>
                </Card>
              ))}
              {!accountDetail.verification_codes.length && <p className="muted-line">没有读取到新的 TG 官方服务验证码时，保持自动轮询等待即可。</p>}
            </div>
          </Card>
        </div>
      )}

      {canClone && accountDetailTab === '克隆' && (
        <Card className="sub-panel compact-panel">
          <div className="section-title">
            <div>
              <h2>账号克隆计划</h2>
              <span>先生成计划，再由操作员确认逐项执行；无法自动完成的项目会标记为需人工处理。</span>
            </div>
            {canClone && <Button type="primary" size="small" onClick={() => {
              onSetCloneForm({ target_account_ids: accounts.filter((item) => item.id !== accountDetail.account.id).slice(0, 2).map((item) => item.id), clone_contacts: true, clone_groups: true });
              onSetModal({ type: 'accountCloneCreate' });
            }}>新建克隆计划</Button>}
          </div>
          <div className="mini-list">
            {accountDetail.clone_plans.map((plan) => (
              <Card key={plan.id} className={statusAccent(plan.status)} size="small">
                <StatusBadge status={plan.status} />
                <strong>计划 #{plan.id}：{accountName(plan.source_account_id)} 到 {plan.target_accounts_summary.map((item) => item.display_name).join('、') || accountName(plan.target_account_id)}</strong>
                <span>总 {plan.items_total} / 完成 {plan.items_done} / 失败 {plan.items_failed}</span>
                <div className="row-actions">
                  <Button size="small" disabled={plan.status === '已完成'} onClick={() => onOpenConfirm({
                    title: '执行克隆计划',
                    message: `确认执行克隆计划 #${plan.id}？平台会逐项添加联系人或加入可处理群聊。`,
                    confirmLabel: '确认执行',
                    restoreModalType: 'accountDetail',
                    onConfirm: () => onConfirmClonePlan(plan),
                  })}>确认执行</Button>
                </div>
                <div className="clone-item-list">
                  {plan.items.slice(0, 8).map((item) => (
                    <span key={item.id} className="inline-status">
                      <StatusBadge status={item.status} label={item.target_display || item.target_peer_id} />
                      {item.status !== '已完成' && <Button size="small" loading={isActionPending(`clone-item:${item.id}:retry`)} onClick={() => onRetryCloneItem(item)}>重试</Button>}
                    </span>
                  ))}
                </div>
              </Card>
            ))}
            {!accountDetail.clone_plans.length && <p className="muted-line">暂无克隆计划。</p>}
          </div>
        </Card>
      )}

      {accountDetailTab === '验证待处理' && (
        <Card className="sub-panel compact-panel">
          <div className="section-title">
            <div>
              <h2>验证辅助</h2>
              <span>账号级受限由系统探测恢复；群管理机器拦截需要管理员在群内解除后，从这里重查目标能力。</span>
            </div>
          </div>
          <div className="mini-list">
            {accountDetail.verification_tasks.map((task) => (
              <Card key={task.id} className={statusAccent(task.status)} size="small">
                <StatusBadge status={task.status} />
                <strong>{task.verification_type}</strong>
                <span>目标：{verificationTargetLabel(task)}</span>
                <span>{task.detected_reason || '等待处理'}</span>
                <span>处理入口：{verificationActionLabel(task)} / {task.issue_category === 'group_restriction' ? '群内管理员解除后重查' : task.suggested_action}</span>
                <div className="row-actions">
                  {task.issue_category === 'group_restriction' ? (
                    <Button size="small" loading={isActionPending(`verification:${task.id}:resolve-group`)} disabled={!verificationActionable(task)} onClick={() => onResolveGroupRestrictionTask(task)}>解除群限制</Button>
                  ) : (
                    <Button size="small" disabled={!verificationActionable(task)} onClick={() => { onSetReturnAfterVerification('accountDetail'); onSetModal({ type: 'verificationTaskDetail', payload: task }); }}>{verificationActionLabel(task)}</Button>
                  )}
                  <Button size="small" loading={isActionPending(`verification:${task.id}:dismiss`)} disabled={!verificationActionable(task)} onClick={() => onDismissVerificationTask(task)}>忽略</Button>
                </div>
              </Card>
            ))}
            {!accountDetail.verification_tasks.length && <p className="muted-line">暂无待处理验证。</p>}
          </div>
        </Card>
      )}

      {(accountDetailTab === '执行记录' || accountDetailTab === '发送记录') && (
        <>
          <div className="cards-grid compact-stats">
            {accountDetail.manual_operation_records.map((record) => (
              <Card key={record.id} size="small" className={statusAccent(record.status)}>
                <StatusBadge status={record.status} />
                <strong>手动发送 #{record.id}</strong>
                <span>{record.content}</span>
                <span>{record.remote_message_id || record.failure_detail || formatBeijingDateTime(record.created_at)}</span>
              </Card>
            ))}
            {accountDetail.operation_task_attempts.map((attempt) => (
              <Card key={`attempt-${attempt.id}`} size="small" className={statusAccent(attempt.status)}>
                <StatusBadge status={attempt.status} />
                <strong>{attempt.action_type} #{attempt.task_id}</strong>
                <span>{attempt.remote_message_id || attempt.failure_detail || (attempt.executed_at ? formatBeijingDateTime(attempt.executed_at) : '待执行')}</span>
              </Card>
            ))}
          </div>
          <Space className="toolbar-row" wrap>
            {messageTable.searchInput}
          </Space>
          <Table<MessageTask>
            className="tg-table"
            rowKey="id"
            columns={messageColumns}
            dataSource={messageTable.filteredRows}
            pagination={messageTable.pagination}
            scroll={{ x: 840 }}
            locale={{ emptyText: '暂无发送记录。' }}
          />
        </>
      )}
      </div>
    </Modal>
  );
}
