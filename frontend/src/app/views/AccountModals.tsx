import React from 'react';
import type {
  Account, AccountPool, AccountDetail, AccountPoolDetail,
  AccountClonePlan, AccountCloneItem, VerificationTask, Contact,
  RuntimeConfig, CurrentUser,
} from '../types';
import { Modal, FormActions, StatusBadge } from '../components/shared';
import { statusAccent, operationLabel, syncTypeLabel } from '../utils';

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
}

export function AccountPoolDetailModal({
  accountPoolDetail, poolDirectAccountId, setPoolDirectAccountId,
  directMessageForm, setDirectMessageForm, selectedDirectContact,
  onClose, onOpenAccountCreate, onOpenAccountDetail,
  onRefreshAccountPoolDetail, onStartDirectMessageToContact,
  onCreateDirectMessageTask, onOpenConfirm, onSetReturnAfterVerification,
  onSetModal, accountName,
}: AccountPoolDetailModalProps) {
  return (
    <Modal title={`${accountPoolDetail.pool.name} 账号池`} size="large" onClose={onClose}>
      <div className="detail-list">
        <div><dt>池内账号</dt><dd>{accountPoolDetail.stats.accounts ?? 0} 个</dd></div>
        <div><dt>在线账号</dt><dd>{accountPoolDetail.stats.online ?? 0} 个</dd></div>
        <div><dt>云联系人</dt><dd>{accountPoolDetail.stats.contacts ?? 0} 个</dd></div>
        <div><dt>待处理验证</dt><dd><StatusBadge status={(accountPoolDetail.stats.verification_tasks ?? 0) ? '待处理' : '已完成'} label={accountPoolDetail.stats.verification_tasks ?? 0} /></dd></div>
      </div>
      <div className="flow-sections">
        <section className="sub-panel compact-panel">
          <div className="section-title">
            <div>
              <h2>账号</h2>
              <span>从池内账号进入详情、登录、同步和资料管理。</span>
            </div>
            <button className="small" onClick={() => onOpenAccountCreate(true)}>新增登录账号</button>
          </div>
          <div className="mini-list">
            {accountPoolDetail.accounts.map((account) => (
              <article key={account.id} className={statusAccent(account.status)}>
                <StatusBadge status={account.status} />
                <strong>{account.display_name}</strong>
                <span>{account.phone_masked} / 健康分 {Math.round(account.health_score)}</span>
                <button className="small" onClick={() => onOpenAccountDetail(account)}>进入账号</button>
              </article>
            ))}
            {!accountPoolDetail.accounts.length && <p className="muted-line">这个账号池还没有账号。</p>}
          </div>
        </section>

        <section className="sub-panel compact-panel">
          <div className="section-title">
            <div>
              <h2>云联系人发送</h2>
              <span>先选择发送账号，再从已同步联系人或群友中选择对象。</span>
            </div>
            <button className="small" onClick={onRefreshAccountPoolDetail}>刷新账号池</button>
          </div>
          <div className="policy-grid">
            <label>发送账号
              <select value={poolDirectAccountId} onChange={(event) => setPoolDirectAccountId(Number(event.target.value) || '')}>
                <option value="">选择发送账号</option>
                {accountPoolDetail.accounts.map((account) => (
                  <option key={account.id} value={account.id} disabled={account.status !== '在线'}>
                    {account.display_name} / {account.status === '在线' ? '可发送' : account.status}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="contact-pick-grid">
            {accountPoolDetail.contacts.filter((contact) => !poolDirectAccountId || contact.account_id === poolDirectAccountId).map((contact) => (
              <button key={contact.id} type="button" className={selectedDirectContact?.id === contact.id ? 'selected contact-pick' : 'contact-pick'} onClick={() => onStartDirectMessageToContact(contact)}>
                <strong>{contact.display_name}</strong>
                <span>{contact.username ? `@${contact.username}` : contact.peer_id}</span>
                <small>{accountName(contact.account_id)} / {contact.contact_type === 'group_member' ? '群友候选' : '私聊对象'}</small>
              </button>
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
            <label className="wide-field">消息内容<textarea value={directMessageForm.content} onChange={(event) => setDirectMessageForm({ ...directMessageForm, content: event.target.value })} /></label>
            <div className="wide-field detail-actions">
              <button className="primary" disabled={!poolDirectAccountId || !selectedDirectContact || !directMessageForm.content} onClick={() => onOpenConfirm({
                title: '创建池内私发任务',
                message: `确认用「${accountName(poolDirectAccountId || null)}」向「${selectedDirectContact?.display_name ?? ''}」发送这条消息？`,
                confirmLabel: '创建并发送',
                restoreModalType: 'accountPoolDetail',
                onConfirm: onCreateDirectMessageTask,
              })}>创建并发送</button>
            </div>
          </div>
        </section>

        <section className="sub-panel compact-panel">
          <div className="section-title">
            <div>
              <h2>克隆和验证</h2>
              <span>查看池内克隆计划与需要人工确认的验证事项。</span>
            </div>
          </div>
          <div className="mini-list">
            {accountPoolDetail.clone_plans.slice(0, 4).map((plan) => (
              <article key={plan.id} className={statusAccent(plan.status)}>
                <StatusBadge status={plan.status} />
                <strong>克隆计划 #{plan.id}</strong>
                <span>{plan.target_accounts_summary.map((item) => item.display_name).join('、') || accountName(plan.target_account_id)}</span>
                <span>总 {plan.items_total} / 完成 {plan.items_done} / 失败 {plan.items_failed}</span>
              </article>
            ))}
            {accountPoolDetail.verification_tasks.slice(0, 4).map((task) => (
              <article key={`v-${task.id}`} className={statusAccent(task.status)}>
                <StatusBadge status={task.status} />
                <strong>{task.verification_type}</strong>
                <span>{task.detected_reason || task.suggested_action}</span>
                <button className="small" onClick={() => { onSetReturnAfterVerification('accountPoolDetail'); onSetModal({ type: 'verificationTaskDetail', payload: task }); }}>处理</button>
              </article>
            ))}
            {!accountPoolDetail.clone_plans.length && !accountPoolDetail.verification_tasks.length && <p className="muted-line">暂无克隆计划或验证事项。</p>}
          </div>
        </section>
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
}

export function AccountDetailModal({
  accountDetail, accountDetailTab, setAccountDetailTab, runtime,
  directMessageForm, setDirectMessageForm, selectedDirectContact,
  accountContacts, accounts, avatarUrl, onClose,
  onOpenAccountProfileEdit, onQueueAccountSyncNow, onPollVerificationCodes,
  onStartDirectMessageToContact, onCreateDirectMessageTask,
  onConfirmClonePlan, onRetryCloneItem,
  onRetryAccountProfileSync,
  onDismissVerificationTask, onConfirmVerificationTask,
  onOpenConfirm, onSetReturnAfterVerification, onSetModal,
  onSetCloneForm, accountName,
}: AccountDetailModalProps) {
  return (
    <Modal title={`${accountDetail.account.display_name} 账号详情`} size="large" onClose={onClose}>
      <div className="detail-list">
        <div><dt>账号状态</dt><dd><StatusBadge status={accountDetail.account.status} /></dd></div>
        <div><dt>所属账号池</dt><dd>{accountDetail.account.pool_name}</dd></div>
        <div><dt>资料同步</dt><dd><StatusBadge status={accountDetail.account.profile_sync_status} /></dd></div>
        <div><dt>加入群聊</dt><dd>{accountDetail.stats.joined_groups ?? 0} 个</dd></div>
        <div><dt>发送记录</dt><dd>{accountDetail.stats.message_records ?? 0} 条</dd></div>
        <div><dt>成功/失败</dt><dd><span className="inline-status"><StatusBadge status="已发送" label={accountDetail.stats.sent ?? 0} /><StatusBadge status={(accountDetail.stats.failed ?? 0) > 0 ? '失败' : '无失败'} label={accountDetail.stats.failed ?? 0} /></span></dd></div>
      </div>
      <div className="tabs-row">
        {['资料', '登录同步', '云联系人', '群聊', '克隆', '验证待处理', '发送记录'].map((tabName) => (
          <button key={tabName} className={accountDetailTab === tabName ? 'active' : ''} onClick={() => setAccountDetailTab(tabName)}>{tabName}</button>
        ))}
      </div>

      {accountDetailTab === '资料' && (
        <div className="sub-panel compact-panel">
          <div className="section-title">
            <div>
              <h2>账号资料</h2>
              <span>平台备注名用于后台识别，TG 昵称、简介和头像会通过同步任务更新到真实账号。</span>
            </div>
            <div className="row-actions">
              <button className="primary small" onClick={onOpenAccountProfileEdit}>编辑资料</button>
              <button className="small" onClick={() => onSetModal({ type: 'accountMovePool' })}>移动账号池</button>
              <button className="small" onClick={() => {
                onSetCloneForm({ target_account_ids: accounts.filter((item) => item.id !== accountDetail.account.id).slice(0, 2).map((item) => item.id), clone_contacts: true, clone_groups: true });
                onSetModal({ type: 'accountCloneCreate' });
              }}>克隆到其他账号</button>
              <button className="small" disabled={accountDetail.account.profile_sync_status !== '失败'} onClick={() => onOpenConfirm({
                title: '重试资料同步',
                message: `确认重新同步「${accountDetail.account.display_name}」的 TG 资料？`,
                confirmLabel: '重新入队',
                restoreModalType: 'accountDetail',
                onConfirm: onRetryAccountProfileSync,
              })}>重试同步</button>
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
              <div><dt>最近同步</dt><dd>{accountDetail.account.profile_synced_at ? new Date(accountDetail.account.profile_synced_at).toLocaleString() : '暂无成功同步'}</dd></div>
            </div>
          </div>
          {accountDetail.account.profile_sync_error && <p className="danger-text">{accountDetail.account.profile_sync_error}</p>}
          <div className="mini-list">
            {accountDetail.profile_sync_records.map((record) => (
              <article key={record.id}>
                <StatusBadge status={record.status} />
                <strong>同步记录 #{record.id}</strong>
                <span>{record.actor || '系统'} / {new Date(record.created_at).toLocaleString()}</span>
                <span>{record.remote_detail || record.failure_detail || '等待处理'}</span>
              </article>
            ))}
            {!accountDetail.profile_sync_records.length && <p className="muted-line">暂无资料同步记录。</p>}
          </div>
        </div>
      )}

      {accountDetailTab === '登录同步' && (
        <div className="sub-panel compact-panel">
          <div className="section-title">
            <div>
              <h2>验证码与登录流程</h2>
              <span>验证码短时展示，查看行为会写入审计</span>
            </div>
            <div className="row-actions">
              <button className="small" onClick={onQueueAccountSyncNow}>同步群聊和联系人</button>
              <button className="small" onClick={onPollVerificationCodes}>查看 TG 官方验证码</button>
            </div>
          </div>
          <div className="mini-list">
            {accountDetail.verification_codes.map((code) => (
              <article key={code.id}>
                <StatusBadge status={code.code_preview ? '可查看' : code.status} label={code.source === 'login_flow' ? '登录验证码' : 'TG 官方验证码'} />
                <strong>{code.code_preview ? `TG 官方验证码 ${code.code_preview}` : code.status}</strong>
                <span>{code.expires_at ? `有效到 ${new Date(code.expires_at).toLocaleTimeString()}` : '等待新的验证码'}</span>
                {runtime?.show_advanced_debug && <small>{code.raw_hint || code.source}</small>}
              </article>
            ))}
            {accountDetail.login_flows.map((flow) => (
              <article key={`flow-${flow.id}`}>
                <StatusBadge status={flow.status} />
                <strong>{flow.method === 'qr' ? '扫码登录' : '验证码登录'}</strong>
                <span>{flow.code_preview ? `登录验证码 ${flow.code_preview}` : flow.qr_payload ? '等待扫码确认' : flow.status}</span>
                {runtime?.show_advanced_debug && <small>流程 #{flow.id}</small>}
              </article>
            ))}
          </div>
          <div className="mini-list">
            {accountDetail.sync_records.map((record) => (
              <article key={`sync-${record.id}`} className={statusAccent(record.status)}>
                <StatusBadge status={record.status} label={syncTypeLabel(record.sync_type)} />
                <strong>{record.status}</strong>
                <span>{record.result_count ? `已同步 ${record.result_count} 条` : record.failure_detail || '等待后台处理'}</span>
                <span>{record.finished_at ? new Date(record.finished_at).toLocaleString() : new Date(record.created_at).toLocaleString()}</span>
              </article>
            ))}
            {accountDetail.next_sync_at && <p className="muted-line">下次自动同步约在 {new Date(accountDetail.next_sync_at).toLocaleString()}</p>}
            {!accountDetail.sync_records.length && <p className="muted-line">登录成功后会自动同步群聊、云联系人和 TG 官方验证码。</p>}
          </div>
        </div>
      )}

      {accountDetailTab === '群聊' && (
        <div className="table">
          {accountDetail.groups.map((group) => (
            <div className={`table-row account-detail-row ${statusAccent(group.account_can_send ? group.auth_status : '账号不可发言')}`} key={group.id}>
              <div>
                <strong>{group.title}</strong>
                <span>{group.member_count.toLocaleString()} 成员 / {group.permission_label}</span>
              </div>
              <StatusBadge status={group.auth_status} label={operationLabel(group.auth_status)} />
              <StatusBadge status={group.account_can_send ? '账号可发言' : '账号不可发言'} />
              <span>{group.last_sent_at ? `上次发送 ${new Date(group.last_sent_at).toLocaleString()}` : '暂无发送'}</span>
            </div>
          ))}
        </div>
      )}

      {accountDetailTab === '云联系人' && (
        <div className="sub-panel compact-panel">
          <div className="section-title">
            <div>
              <h2>云联系人</h2>
              <span>从当前账号同步的私聊对象和群友中选择，直接创建平台发送任务。</span>
            </div>
            <button className="small" onClick={onQueueAccountSyncNow}>同步对象</button>
          </div>
          <div className="contact-pick-grid">
            {accountContacts.map((contact) => (
              <button key={contact.id} type="button" className={selectedDirectContact?.id === contact.id ? 'selected contact-pick' : 'contact-pick'} onClick={() => onStartDirectMessageToContact(contact)}>
                <strong>{contact.display_name}</strong>
                <span>{contact.username ? `@${contact.username}` : contact.peer_id}</span>
                <small>{contact.contact_type === 'group_member' ? '群友候选' : '私聊对象'}{contact.is_mutual ? ' / 双向联系人' : ''}{contact.phone_masked ? ` / ${contact.phone_masked}` : ''}</small>
              </button>
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
            <label className="wide-field">消息内容<textarea value={directMessageForm.content} onChange={(event) => setDirectMessageForm({ ...directMessageForm, content: event.target.value })} /></label>
            <div className="wide-field detail-actions">
              <button className="primary" disabled={!selectedDirectContact || !directMessageForm.content} onClick={() => onOpenConfirm({
                title: '创建私发消息任务',
                message: `确认使用「${accountDetail.account.display_name}」向「${selectedDirectContact?.display_name ?? directMessageForm.target_display}」创建平台发送任务？`,
                confirmLabel: '创建并发送',
                restoreModalType: 'accountDetail',
                onConfirm: onCreateDirectMessageTask,
              })}>创建并发送</button>
            </div>
          </div>
        </div>
      )}

      {accountDetailTab === '克隆' && (
        <div className="sub-panel compact-panel">
          <div className="section-title">
            <div>
              <h2>账号克隆计划</h2>
              <span>先生成计划，再由操作员确认逐项执行；无法自动完成的项目会标记为需人工处理。</span>
            </div>
            <button className="primary small" onClick={() => {
              onSetCloneForm({ target_account_ids: accounts.filter((item) => item.id !== accountDetail.account.id).slice(0, 2).map((item) => item.id), clone_contacts: true, clone_groups: true });
              onSetModal({ type: 'accountCloneCreate' });
            }}>新建克隆计划</button>
          </div>
          <div className="mini-list">
            {accountDetail.clone_plans.map((plan) => (
              <article key={plan.id} className={statusAccent(plan.status)}>
                <StatusBadge status={plan.status} />
                <strong>计划 #{plan.id}：{accountName(plan.source_account_id)} 到 {plan.target_accounts_summary.map((item) => item.display_name).join('、') || accountName(plan.target_account_id)}</strong>
                <span>总 {plan.items_total} / 完成 {plan.items_done} / 失败 {plan.items_failed}</span>
                <div className="row-actions">
                  <button className="small" disabled={plan.status === '已完成'} onClick={() => onOpenConfirm({
                    title: '执行克隆计划',
                    message: `确认执行克隆计划 #${plan.id}？平台会逐项添加联系人或加入可处理群聊。`,
                    confirmLabel: '确认执行',
                    restoreModalType: 'accountDetail',
                    onConfirm: () => onConfirmClonePlan(plan),
                  })}>确认执行</button>
                </div>
                <div className="clone-item-list">
                  {plan.items.slice(0, 8).map((item) => (
                    <span key={item.id} className="inline-status">
                      <StatusBadge status={item.status} label={item.target_display || item.target_peer_id} />
                      {item.status !== '已完成' && <button className="tiny-button" onClick={() => onRetryCloneItem(item)}>重试</button>}
                    </span>
                  ))}
                </div>
              </article>
            ))}
            {!accountDetail.clone_plans.length && <p className="muted-line">暂无克隆计划。</p>}
          </div>
        </div>
      )}

      {accountDetailTab === '验证待处理' && (
        <div className="sub-panel compact-panel">
          <div className="section-title">
            <div>
              <h2>验证辅助</h2>
              <span>遇到关注频道、机器人按钮、发言验证等情况时，平台会生成可确认的处理事项。</span>
            </div>
          </div>
          <div className="mini-list">
            {accountDetail.verification_tasks.map((task) => (
              <article key={task.id} className={statusAccent(task.status)}>
                <StatusBadge status={task.status} />
                <strong>{task.verification_type}</strong>
                <span>{task.detected_reason || '等待处理'}</span>
                <span>建议操作：{task.suggested_action}</span>
                <div className="row-actions">
                  <button className="small" disabled={!['待处理', '失败'].includes(task.status)} onClick={() => { onSetReturnAfterVerification('accountDetail'); onSetModal({ type: 'verificationTaskDetail', payload: task }); }}>处理</button>
                  <button className="small" disabled={task.status !== '待处理'} onClick={() => onDismissVerificationTask(task)}>忽略</button>
                </div>
              </article>
            ))}
            {!accountDetail.verification_tasks.length && <p className="muted-line">暂无待处理验证。</p>}
          </div>
        </div>
      )}

      {accountDetailTab === '发送记录' && (
        <div className="table">
          {accountDetail.message_records.map((task) => (
            <div className={`table-row account-detail-row ${statusAccent(task.status)}`} key={task.id}>
              <div>
                <strong>任务 #{task.id}</strong>
                <span>{task.target_type === 'private' ? `私发：${task.target_display}` : `群任务：${task.group_id}`}</span>
                <span>{task.content}</span>
              </div>
              <StatusBadge status={task.status} />
              <StatusBadge status={task.failure_type ?? '无失败'} />
              <span>{task.sent_at ? new Date(task.sent_at).toLocaleString() : new Date(task.scheduled_at).toLocaleString()}</span>
            </div>
          ))}
        </div>
      )}
    </Modal>
  );
}
