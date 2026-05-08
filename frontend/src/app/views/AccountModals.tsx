import React from 'react';
import { Alert, Button, Card, Descriptions, Empty, Input, List, Select, Space, Table, Tabs, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type {
  Account, AccountPool, AccountDetail, AccountPoolDetail,
  AccountClonePlan, AccountCloneItem, VerificationTask, Contact,
  RuntimeConfig, CurrentUser, AccountGroup, MessageTask,
} from '../types';
import { Modal, FormActions, StatusBadge, useAntdTableControls } from '../components/shared';
import { statusAccent, operationLabel, syncTypeLabel } from '../utils';
import { api } from '../../shared/api/client';

const accountPhone = (account: Account) => account.phone_number || account.phone_masked;

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
}

export function AccountPoolDetailModal({
  accountPoolDetail, poolDirectAccountId, setPoolDirectAccountId,
  directMessageForm, setDirectMessageForm, selectedDirectContact,
  onClose, onOpenAccountCreate, onOpenAccountDetail,
  onRefreshAccountPoolDetail, onStartDirectMessageToContact,
  onCreateDirectMessageTask, onOpenConfirm, onSetReturnAfterVerification,
  onSetModal, accountName, isActionPending,
}: AccountPoolDetailModalProps) {
  return (
    <Modal title={`${accountPoolDetail.pool.name} 账号分组`} size="large" onClose={onClose}>
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
        <Card className="sub-panel compact-panel" title="账号" extra={<Button size="small" onClick={() => onOpenAccountCreate(true)}>新增账号</Button>}>
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
              <Button type="primary" disabled={!poolDirectAccountId || !selectedDirectContact || !directMessageForm.content} onClick={() => onOpenConfirm({
                title: '创建池内私发任务',
                message: `确认用「${accountName(poolDirectAccountId || null)}」向「${selectedDirectContact?.display_name ?? ''}」发送这条消息？`,
                confirmLabel: '创建并发送',
                restoreModalType: 'accountPoolDetail',
                onConfirm: onCreateDirectMessageTask,
              })}>创建并发送</Button>
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
              <List.Item className={statusAccent(task.status)} actions={[<Button size="small" onClick={() => { onSetReturnAfterVerification('accountPoolDetail'); onSetModal({ type: 'verificationTaskDetail', payload: task }); }}>处理</Button>]}>
                <List.Item.Meta
                  title={<Space><StatusBadge status={task.status} />{task.verification_type}</Space>}
                  description={task.detected_reason || task.suggested_action}
                />
              </List.Item>
            )}
          />
        </Card>
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
  onPollVerificationCodes: () => Promise<void>;
  onStartDirectMessageToContact: (contact: Contact) => void;
  onCreateDirectMessageTask: () => Promise<void>;
  onConfirmClonePlan: (plan: AccountClonePlan) => Promise<void>;
  onRetryCloneItem: (item: AccountCloneItem) => Promise<void>;
  onRetryAccountProfileSync: () => Promise<void>;
  onDismissVerificationTask: (task: VerificationTask) => Promise<void>;
  onConfirmVerificationTask: (task: VerificationTask) => Promise<void>;
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
  onDismissVerificationTask, onConfirmVerificationTask,
  onOpenConfirm, onSetReturnAfterVerification, onSetModal,
  onSetCloneForm, accountName, isActionPending,
}: AccountDetailModalProps) {
  const [manualTargetId, setManualTargetId] = React.useState<number | null>(null);
  const [manualContent, setManualContent] = React.useState('');
  const [manualSending, setManualSending] = React.useState(false);

  React.useEffect(() => {
    setManualTargetId((current) => current ?? accountDetail.operation_targets[0]?.id ?? null);
  }, [accountDetail.operation_targets]);

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
    { title: '最近发送', key: 'last_sent_at', width: 200, render: (_, group) => group.last_sent_at ? new Date(group.last_sent_at).toLocaleString() : '暂无发送' },
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
    { title: '时间', key: 'time', width: 200, render: (_, task) => task.sent_at ? new Date(task.sent_at).toLocaleString() : new Date(task.scheduled_at).toLocaleString() },
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

  return (
    <Modal title={`${accountDetail.account.display_name} 账号详情`} size="large" onClose={onClose}>
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
      <Tabs
        className="tabs-row"
        activeKey={accountDetailTab}
        onChange={setAccountDetailTab}
        items={['资料', '官方验证码', '群/频道', '立即发送', '云联系人', '克隆', '验证待处理', '执行记录'].map((tabName) => ({ key: tabName, label: tabName }))}
      />

      {accountDetailTab === '资料' && (
        <Card className="sub-panel compact-panel">
          <div className="section-title">
            <div>
              <h2>账号资料</h2>
              <span>平台备注名用于后台识别，TG 昵称、简介和头像会通过同步任务更新到真实账号。</span>
            </div>
            <div className="row-actions">
              <Button type="primary" size="small" onClick={onOpenAccountProfileEdit}>编辑资料</Button>
              <Button size="small" onClick={() => onSetModal({ type: 'accountMovePool' })}>移动账号分组</Button>
              <Button size="small" onClick={() => {
                onSetCloneForm({ target_account_ids: accounts.filter((item) => item.id !== accountDetail.account.id).slice(0, 2).map((item) => item.id), clone_contacts: true, clone_groups: true });
                onSetModal({ type: 'accountCloneCreate' });
              }}>克隆到其他账号</Button>
              <Button size="small" loading={isActionPending(`account:${accountDetail.account.id}:profile-sync`)} disabled={accountDetail.account.profile_sync_status !== '失败'} onClick={() => onOpenConfirm({
                title: '重试资料同步',
                message: `确认重新同步「${accountDetail.account.display_name}」的 TG 资料？`,
                confirmLabel: '重新入队',
                restoreModalType: 'accountDetail',
                onConfirm: onRetryAccountProfileSync,
              })}>重试同步</Button>
            </div>
          </div>
          <div className="mini-list">
            {accountDetail.risk_diagnostics.length ? accountDetail.risk_diagnostics.map((risk) => (
              <Alert
                key={`${risk.code}-${risk.source}-${risk.title}`}
                type={risk.level === '高' ? 'error' : risk.level === '中' ? 'warning' : 'info'}
                showIcon
                message={`${risk.level}风险：${risk.title}`}
                description={`${risk.detail} 建议：${risk.action}${risk.occurred_at ? ` / ${new Date(risk.occurred_at).toLocaleString()}` : ''}`}
              />
            )) : (
              <Alert type="success" showIcon message="账号风险正常" description="当前没有受限、封禁、FloodWait、目标不可访问或待处理验证信号。" />
            )}
          </div>
          <div className="profile-layout">
            <div className="avatar-preview">
              {accountDetail.account.avatar_preview_url ? <img src={avatarUrl(accountDetail.account.avatar_preview_url)} alt="" /> : <span>{accountDetail.account.display_name.slice(0, 1)}</span>}
            </div>
            <div className="detail-list">
              <div><dt>平台备注名</dt><dd>{accountDetail.account.display_name}</dd></div>
              <div><dt>TG 昵称</dt><dd>{[accountDetail.account.tg_first_name, accountDetail.account.tg_last_name].filter(Boolean).join(' ') || '未设置'}</dd></div>
              <div><dt>TG 简介</dt><dd>{accountDetail.account.tg_bio || '未设置'}</dd></div>
              <div><dt>最近同步</dt><dd>{accountDetail.account.profile_synced_at ? new Date(accountDetail.account.profile_synced_at).toLocaleString() : '暂无成功同步'}</dd></div>
            </div>
          </div>
          {accountDetail.account.profile_sync_error && <p className="danger-text">{accountDetail.account.profile_sync_error}</p>}
          <div className="mini-list">
            {accountDetail.profile_sync_records.map((record) => (
              <Card key={record.id} size="small">
                <StatusBadge status={record.status} />
                <strong>同步记录 #{record.id}</strong>
                <span>{record.actor || '系统'} / {new Date(record.created_at).toLocaleString()}</span>
                <span>{record.remote_detail || record.failure_detail || '等待处理'}</span>
              </Card>
            ))}
            {!accountDetail.profile_sync_records.length && <p className="muted-line">暂无资料同步记录。</p>}
          </div>
        </Card>
      )}

      {(accountDetailTab === '官方验证码' || accountDetailTab === '登录同步') && (
        <Card className="sub-panel compact-panel">
          <div className="section-title">
            <div>
              <h2>验证码与登录流程</h2>
              <span>验证码短时展示，查看行为会写入审计</span>
            </div>
            <div className="row-actions">
              <Button size="small" loading={isActionPending(`account:${accountDetail.account.id}:detail-refresh`)} onClick={onRefreshAccountDetail}>刷新同步状态</Button>
              <Button size="small" type="primary" loading={isActionPending(`account:${accountDetail.account.id}:sync`)} onClick={onQueueAccountSyncNow}>立即全量同步</Button>
              <Button size="small" onClick={syncTargets}>同步群/频道目标</Button>
              <Button size="small" loading={isActionPending(`account:${accountDetail.account.id}:codes`)} onClick={onPollVerificationCodes}>查看 TG 官方验证码</Button>
            </div>
          </div>
          <div className="mini-list">
            {accountDetail.verification_codes.map((code) => (
              <Card key={code.id} size="small">
                <StatusBadge status={code.code_preview ? '可查看' : code.status} label={code.source === 'login_flow' ? '登录验证码' : 'TG 官方验证码'} />
                <strong>{code.code_preview ? `TG 官方验证码 ${code.code_preview}` : code.status}</strong>
                <span>{code.expires_at ? `有效到 ${new Date(code.expires_at).toLocaleTimeString()}` : '等待新的验证码'}</span>
                {runtime?.show_advanced_debug && <small>{code.raw_hint || code.source}</small>}
              </Card>
            ))}
            {accountDetail.login_flows.map((flow) => (
              <Card key={`flow-${flow.id}`} size="small">
                <StatusBadge status={flow.status} />
                <strong>{flow.method === 'qr' ? '扫码登录' : '验证码登录'}</strong>
                <span>{flow.code_preview ? `登录验证码 ${flow.code_preview}` : flow.qr_payload ? '等待扫码确认' : flow.status}</span>
                {runtime?.show_advanced_debug && <small>流程 #{flow.id}</small>}
              </Card>
            ))}
          </div>
          <div className="mini-list">
            {accountDetail.sync_records.map((record) => (
              <Card key={`sync-${record.id}`} className={statusAccent(record.status)} size="small">
                <StatusBadge status={record.status} label={syncTypeLabel(record.sync_type)} />
                <strong>{record.status}</strong>
                <span>{record.result_count ? `已同步 ${record.result_count} 条` : record.failure_detail || '等待后台处理'}</span>
                <span>{record.finished_at ? new Date(record.finished_at).toLocaleString() : new Date(record.created_at).toLocaleString()}</span>
              </Card>
            ))}
            {accountDetail.next_sync_at && <p className="muted-line">下次自动同步约在 {new Date(accountDetail.next_sync_at).toLocaleString()}</p>}
            {!accountDetail.sync_records.length && <p className="muted-line">登录成功后会自动同步资料、健康、群聊、云联系人和 TG 官方验证码。</p>}
          </div>
        </Card>
      )}

      {(accountDetailTab === '群/频道' || accountDetailTab === '群聊') && (
        <>
          <Space className="toolbar-row" wrap>
            {groupTable.searchInput}
            <Button onClick={syncTargets}>同步群/频道目标</Button>
          </Space>
          <div className="cards-grid compact-stats">
            {accountDetail.operation_targets.map((target) => (
              <Card key={target.id} size="small" className="summary-card">
                <span>{target.target_type === 'channel' ? '频道' : '群聊'}</span>
                <strong>{target.title}</strong>
                <p>{target.tg_peer_id}{target.username ? ` / @${target.username}` : ''}</p>
                <StatusBadge status={target.can_send ? '可发送' : '只读'} label={target.auth_status} />
              </Card>
            ))}
            {!accountDetail.operation_targets.length && <p className="muted-line">暂无群/频道目标。可以先同步账号目标。</p>}
          </div>
          <Table<AccountGroup>
            className="tg-table"
            rowKey="id"
            columns={groupColumns}
            dataSource={groupTable.filteredRows}
            pagination={groupTable.pagination}
            scroll={{ x: 780 }}
            locale={{ emptyText: '暂无群聊记录。' }}
          />
        </>
      )}

      {accountDetailTab === '立即发送' && (
        <Card className="sub-panel compact-panel">
          <div className="section-title">
            <div>
              <h2>立即发送</h2>
              <span>直接使用当前 TG 账号向群聊或频道发送文本，emoji 会按普通 Unicode 文本发送。</span>
            </div>
            <Button size="small" onClick={syncTargets}>同步群/频道目标</Button>
          </div>
          <div className="policy-grid">
            <label className="wide-field">
              目标
              <Select
                value={manualTargetId ?? undefined}
                onChange={(value) => setManualTargetId(value)}
                options={accountDetail.operation_targets.map((target) => ({ value: target.id, label: `${target.target_type === 'channel' ? '频道' : '群聊'} / ${target.title} / ${target.can_send ? '可发送' : '只读'}`, disabled: !target.can_send }))}
              />
            </label>
            <label className="wide-field">消息内容<Input.TextArea rows={4} value={manualContent} onChange={(event) => setManualContent(event.target.value)} placeholder="支持文本和 emoji，例如：今天活动开始啦 👍🔥" /></label>
            <div className="wide-field detail-actions">
              <Button type="primary" loading={manualSending} disabled={!manualTargetId || !manualContent.trim()} onClick={manualSendNow}>立即发送</Button>
            </div>
          </div>
        </Card>
      )}

      {accountDetailTab === '云联系人' && (
        <Card className="sub-panel compact-panel">
          <div className="section-title">
            <div>
              <h2>云联系人</h2>
              <span>从当前账号同步的私聊对象和群友中选择，直接创建平台发送任务。</span>
            </div>
            <Space>
              <Button size="small" loading={isActionPending(`account:${accountDetail.account.id}:detail-refresh`)} onClick={onRefreshAccountDetail}>刷新</Button>
              <Button size="small" type="primary" loading={isActionPending(`account:${accountDetail.account.id}:sync`)} onClick={onQueueAccountSyncNow}>同步并刷新</Button>
            </Space>
          </div>
          <div className="contact-pick-grid">
            {accountContacts.map((contact) => (
              <Button key={contact.id} className={selectedDirectContact?.id === contact.id ? 'selected contact-pick' : 'contact-pick'} onClick={() => onStartDirectMessageToContact(contact)}>
                <strong>{contact.display_name}</strong>
                <span>{contact.username ? `@${contact.username}` : contact.peer_id}</span>
                <small>{contact.contact_type === 'group_member' ? '群友候选' : '私聊对象'}{contact.is_mutual ? ' / 双向联系人' : ''}{contact.phone_masked ? ` / ${contact.phone_masked}` : ''}</small>
              </Button>
            ))}
          </div>
          {!accountContacts.length && <p className="muted-line">还没有可选对象，请先同步云联系人。</p>}
          <div className="policy-grid">
            <div className="wide-field selected-recipient-box">
              <span>当前发送对象</span>
              {selectedDirectContact ? (
                <strong>{selectedDirectContact.display_name} {selectedDirectContact.username ? `@${selectedDirectContact.username}` : ''}</strong>
              ) : (
                <strong>请先选择联系人或群友</strong>
              )}
            </div>
            <label className="wide-field">消息内容<Input.TextArea value={directMessageForm.content} onChange={(event) => setDirectMessageForm({ ...directMessageForm, content: event.target.value })} /></label>
            <div className="wide-field detail-actions">
              <Button type="primary" disabled={!selectedDirectContact || !directMessageForm.content} onClick={() => onOpenConfirm({
                title: '创建私发消息任务',
                message: `确认使用「${accountDetail.account.display_name}」向「${selectedDirectContact?.display_name ?? directMessageForm.target_display}」创建平台发送任务？`,
                confirmLabel: '创建并发送',
                restoreModalType: 'accountDetail',
                onConfirm: onCreateDirectMessageTask,
              })}>创建并发送</Button>
            </div>
          </div>
        </Card>
      )}

      {accountDetailTab === '克隆' && (
        <Card className="sub-panel compact-panel">
          <div className="section-title">
            <div>
              <h2>账号克隆计划</h2>
              <span>先生成计划，再由操作员确认逐项执行；无法自动完成的项目会标记为需人工处理。</span>
            </div>
            <Button type="primary" size="small" onClick={() => {
              onSetCloneForm({ target_account_ids: accounts.filter((item) => item.id !== accountDetail.account.id).slice(0, 2).map((item) => item.id), clone_contacts: true, clone_groups: true });
              onSetModal({ type: 'accountCloneCreate' });
            }}>新建克隆计划</Button>
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
              <span>遇到关注频道、机器人按钮、发言验证等情况时，平台会生成可确认的处理事项。</span>
            </div>
          </div>
          <div className="mini-list">
            {accountDetail.verification_tasks.map((task) => (
              <Card key={task.id} className={statusAccent(task.status)} size="small">
                <StatusBadge status={task.status} />
                <strong>{task.verification_type}</strong>
                <span>{task.detected_reason || '等待处理'}</span>
                <span>建议操作：{task.suggested_action}</span>
                <div className="row-actions">
                  <Button size="small" disabled={!['待处理', '失败'].includes(task.status)} onClick={() => { onSetReturnAfterVerification('accountDetail'); onSetModal({ type: 'verificationTaskDetail', payload: task }); }}>处理</Button>
                  <Button size="small" loading={isActionPending(`verification:${task.id}:dismiss`)} disabled={task.status !== '待处理'} onClick={() => onDismissVerificationTask(task)}>忽略</Button>
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
                <span>{record.remote_message_id || record.failure_detail || new Date(record.created_at).toLocaleString()}</span>
              </Card>
            ))}
            {accountDetail.operation_task_attempts.map((attempt) => (
              <Card key={`attempt-${attempt.id}`} size="small" className={statusAccent(attempt.status)}>
                <StatusBadge status={attempt.status} />
                <strong>{attempt.action_type} #{attempt.task_id}</strong>
                <span>{attempt.remote_message_id || attempt.failure_detail || (attempt.executed_at ? new Date(attempt.executed_at).toLocaleString() : '待执行')}</span>
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
    </Modal>
  );
}
