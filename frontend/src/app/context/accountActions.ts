import type { Dispatch, SetStateAction } from 'react';
import { API_ORIGIN, api } from '../../shared/api/client';
import {
  EMPTY_ACCOUNT_LOGIN_FORM,
  defaultAccountCreateForm,
  defaultAccountPoolForm,
} from './defaults';
import type {
  Account,
  AccountCloneItem,
  AccountClonePlan,
  AccountDetail,
  AccountLoginForm,
  AccountPool,
  AccountPoolDetail,
  AccountSyncRecord,
  Contact,
  CurrentUser,
  Group,
  GroupRestrictionBatchResult,
  GroupDetail,
  LoginFlow,
  ModalState,
  ProfileSyncRecord,
  RuntimeConfig,
  VerificationCode,
  VerificationTask,
} from '../types';

interface AccountActionParams {
  accountCreateForm: ReturnType<typeof defaultAccountCreateForm>;
  accountDetail: AccountDetail | null;
  accountLoginForm: AccountLoginForm;
  accountPoolDetail: AccountPoolDetail | null;
  accountPoolForm: ReturnType<typeof defaultAccountPoolForm>;
  accountPools: AccountPool[];
  avatarFile: File | null;
  cloneForm: { target_account_ids: number[]; clone_contacts: boolean; clone_groups: boolean };
  currentUser: CurrentUser | null;
  groupDetail: GroupDetail | null;
  poolDirectAccountId: number | '';
  profileForm: { display_name: string; tg_first_name: string; tg_last_name: string; tg_bio: string; avatar_object_key: string };
  runtime: RuntimeConfig | null;
  selectedPoolId: number | '';
  choosePoolSendAccount: (detail: AccountPoolDetail) => Account | undefined;
  closeModal: () => void;
  errorMessage: (error: unknown) => string;
  goToView: (viewId: string) => void;
  handleActionError: (error: unknown) => void;
  refresh: () => Promise<void>;
  runWithLoading: <T>(key: string, busyLabel: string, task: () => Promise<T>) => Promise<T>;
  setAccountCreateForm: (form: AccountActionParams['accountCreateForm']) => void;
  setAccountDetail: Dispatch<SetStateAction<AccountDetail | null>>;
  setAccountDetailTab: (tab: string) => void;
  setAccountLoginForm: Dispatch<SetStateAction<AccountLoginForm>>;
  setAccountPoolDetail: (detail: AccountPoolDetail | null) => void;
  setAccountPoolForm: (form: AccountActionParams['accountPoolForm']) => void;
  setAvatarFile: (file: File | null) => void;
  setBusy: (busy: string) => void;
  setDirectMessageForm: (form: { target_peer_id: string; target_display: string; content: string }) => void;
  setGroupDetail: (detail: GroupDetail | null) => void;
  setLoginAfterCreate: (login: boolean) => void;
  setModal: (modal: ModalState) => void;
  setNotice: (notice: string) => void;
  setPoolDirectAccountId: (id: number | '') => void;
  setProfileForm: (form: AccountActionParams['profileForm']) => void;
  setSelectedGroupId: (id: number | null) => void;
  setSelectedPoolId: (id: number | '') => void;
  showResult: (title: string, detail: string) => void;
}

export function createAccountActions(params: AccountActionParams) {
  function openAccountCreate(loginNow = false) {
    if (!params.runtime?.can_create_tg_account) {
      params.goToView('systemConfig');
      params.showResult('请先配置开发者应用', '新增 TG 账号前，需要先在开发者应用中配置可用的 Telegram api_id/api_hash。');
      return;
    }
    params.setLoginAfterCreate(loginNow);
    params.setAccountCreateForm({
      display_name: '',
      username: '',
      phone_number: '',
      pool_id: params.selectedPoolId || params.accountPools.find((pool) => pool.is_default)?.id || params.accountPools[0]?.id || '',
      login_method: 'code',
    });
    params.setModal({ type: 'accountCreate' });
  }

  async function openAccountDetail(account: Account) {
    params.setBusy('读取账号详情');
    const detail = await api<AccountDetail>(`/tg-accounts/${account.id}/detail`);
    params.setAccountDetail(detail);
    params.setDirectMessageForm({ target_peer_id: '', target_display: '', content: '' });
    params.setAccountDetailTab('资料');
    params.setModal({ type: 'accountDetail' });
    params.setBusy('');
  }

  async function openAccountVerificationCodes(account: Account) {
    params.setBusy('提取验证码');
    const detail = await api<AccountDetail>(`/tg-accounts/${account.id}/detail`);
    params.setAccountDetail(detail);
    params.setDirectMessageForm({ target_peer_id: '', target_display: '', content: '' });
    params.setAccountDetailTab('TG 官方验证码');
    params.setModal({ type: 'accountDetail' });
    params.setNotice('请填写查看原因后同步提取 TG 官方验证码。');
    params.setBusy('');
  }

  async function openAccountMovePool(account: Account) {
    params.setBusy('移动账号分组');
    const detail = await api<AccountDetail>(`/tg-accounts/${account.id}/detail`);
    params.setAccountDetail(detail);
    params.setModal({ type: 'accountMovePool' });
    params.setBusy('');
  }

  async function openAccountPoolDetail(pool: AccountPool) {
    params.setBusy('读取账号分组详情');
    const detail = await api<AccountPoolDetail>(`/account-pools/${pool.id}/detail`);
    const defaultAccount = params.choosePoolSendAccount(detail);
    params.setAccountPoolDetail(detail);
    params.setPoolDirectAccountId(defaultAccount?.id || '');
    params.setDirectMessageForm({ target_peer_id: '', target_display: '', content: '' });
    params.setModal({ type: 'accountPoolDetail' });
    params.setBusy('');
  }

  async function refreshAccountPoolDetail() {
    if (!params.accountPoolDetail) return;
    const detail = await api<AccountPoolDetail>(`/account-pools/${params.accountPoolDetail.pool.id}/detail`);
    const selectedAccount = detail.accounts.find((account) => account.id === params.poolDirectAccountId);
    const defaultAccount = params.choosePoolSendAccount(detail);
    params.setAccountPoolDetail(detail);
    if (!selectedAccount || selectedAccount.status !== '在线') {
      params.setPoolDirectAccountId(defaultAccount?.id || '');
    }
  }

  function latestUsableCodeFlow(detail: AccountDetail) {
    return detail.login_flows.find((flow) => (
      flow.method === 'code'
      && flow.status === '等待验证码'
      && (!flow.code_expires_at || new Date(flow.code_expires_at).getTime() > Date.now())
    )) ?? null;
  }

  async function startOrResumeAccountLogin(account: Account, method: 'code' | 'qr' = 'code', resend = false) {
    const isQr = method === 'qr';
    const actionKey = resend ? `account-login:${account.id}:resend` : `account-login:${account.id}:${method}`;
    const busyLabel = isQr ? '启动扫码登录' : resend ? '重新发送验证码' : '启动登录';
    return params.runWithLoading(actionKey, busyLabel, async () => {
      params.setModal({ type: 'accountLogin' });
      params.setAccountLoginForm({
        ...EMPTY_ACCOUNT_LOGIN_FORM,
        account,
        method,
        step: account.status === '等待2FA' ? 'password' : isQr || account.status === '等待扫码' ? 'qr' : 'code',
      });
      let flow: LoginFlow | null = null;
      if (!resend && account.status === '等待验证码' && method === 'code') {
        const detail = await api<AccountDetail>(`/tg-accounts/${account.id}/detail`);
        flow = latestUsableCodeFlow(detail);
        if (params.accountDetail?.account.id === account.id) {
          params.setAccountDetail(detail);
        }
      }
      if (!flow && account.status !== '等待2FA') {
        flow = await api<LoginFlow>(`/tg-accounts/${account.id}/login/start`, {
          method: 'POST',
          body: JSON.stringify({ method }),
        });
      }
      const nextAccount = flow ? { ...account, status: flow.status } : account;
      params.setAccountLoginForm((current) => ({
        ...current,
        account: nextAccount,
        method,
        step: nextAccount.status === '等待2FA' ? 'password' : method === 'qr' || nextAccount.status === '等待扫码' ? 'qr' : 'code',
        flow,
        error: '',
      }));
      params.setNotice(isQr ? '请使用 Telegram 扫码确认登录。' : resend ? '已重新发送登录验证码。' : '请完成验证码登录。');
      await params.refresh();
      if (params.accountDetail?.account.id === account.id) await refreshAccountDetail();
    }).catch((error) => {
      params.setAccountLoginForm((current) => ({ ...current, error: params.errorMessage(error) }));
    });
  }

  async function completeAccountLogin(updated: Account) {
    params.setAccountLoginForm((current) => ({ ...current, account: updated, error: '' }));
    await params.refresh();
    if (updated.status === '等待2FA') {
      params.setAccountLoginForm((current) => ({ ...current, account: updated, step: 'password', code: '', error: '' }));
      params.setNotice('验证码已通过，请输入 Telegram 二步验证密码。');
      return;
    }
    if (updated.status !== '在线') {
      params.setAccountLoginForm((current) => ({ ...current, account: updated, error: `登录未完成，当前状态：${updated.status}` }));
      return;
    }
    params.setAccountLoginForm(EMPTY_ACCOUNT_LOGIN_FORM);
    params.closeModal();
    params.setNotice(`${updated.display_name} 已完成登录，并已同步资料、健康、群聊、联系人和验证码。`);
  }

  async function createAccount() {
    params.setBusy('添加账号');
    try {
      const created = await api<Account>('/tg-accounts', {
        method: 'POST',
        body: JSON.stringify({
          tenant_id: params.currentUser?.tenant_id ?? 1,
          pool_id: params.accountCreateForm.pool_id || null,
          display_name: params.accountCreateForm.display_name,
          username: params.accountCreateForm.username || null,
          phone_number: params.accountCreateForm.phone_number,
        }),
      });
      params.setAccountCreateForm({ display_name: '', username: '', phone_number: '', pool_id: '', login_method: 'code' });
      await params.refresh();
      await startOrResumeAccountLogin(created, params.accountCreateForm.login_method, params.accountCreateForm.login_method === 'code');
    } catch (error) {
      params.handleActionError(error);
    } finally {
      params.setBusy('');
    }
  }

  async function deleteAccount(account: Account) {
    params.setBusy('移除账号');
    try {
      const removed = await api<Account>(`/tg-accounts/${account.id}`, { method: 'DELETE' });
      if (params.accountLoginForm.account?.id === removed.id) {
        params.setAccountLoginForm(EMPTY_ACCOUNT_LOGIN_FORM);
      }
      if (params.accountDetail?.account.id === removed.id) {
        params.setAccountDetail(null);
      }
      await params.refresh();
      params.setNotice(`${removed.display_name} 已移除，历史任务和归档记录仍会保留。`);
    } catch (error) {
      params.handleActionError(error);
    } finally {
      params.setBusy('');
    }
  }

  async function createAccountPool() {
    params.setBusy('新增账号分组');
    const pool = await api<AccountPool>('/account-pools', {
      method: 'POST',
      body: JSON.stringify({ tenant_id: params.currentUser?.tenant_id ?? 1, ...params.accountPoolForm }),
    });
    params.closeModal();
    params.showResult('账号分组已新增', `已新增账号分组：${pool.name}`);
    params.setAccountPoolForm({ name: '新账号分组', description: '', is_default: false });
    params.setSelectedPoolId(pool.id);
    await params.refresh();
    params.setBusy('');
  }

  async function moveCurrentAccountPool(poolId: number) {
    if (!params.accountDetail) return;
    params.setBusy('移动账号分组');
    const updated = await api<Account>(`/tg-accounts/${params.accountDetail.account.id}/move-pool`, {
      method: 'POST',
      body: JSON.stringify({ pool_id: poolId }),
    });
    params.showResult('账号分组已更新', `${updated.display_name} 已移动到 ${updated.pool_name}`);
    await params.refresh();
    await refreshAccountDetail();
    params.setModal({ type: 'accountDetail' });
    params.setBusy('');
  }

  async function createClonePlan() {
    if (!params.accountDetail || !params.cloneForm.target_account_ids.length) return;
    params.setBusy('创建克隆计划');
    const clone_scope = [
      params.cloneForm.clone_contacts ? 'contacts' : '',
      params.cloneForm.clone_groups ? 'groups' : '',
    ].filter(Boolean);
    const plan = await api<AccountClonePlan>('/account-clone-plans', {
      method: 'POST',
      body: JSON.stringify({
        tenant_id: params.currentUser?.tenant_id ?? 1,
        source_account_id: params.accountDetail.account.id,
        target_account_ids: params.cloneForm.target_account_ids,
        clone_scope,
      }),
    });
    params.showResult('克隆计划已生成', `已生成 ${plan.items_total} 个克隆项，请确认后执行。`);
    await refreshAccountDetail();
    params.setAccountDetailTab('克隆');
    params.setModal({ type: 'accountDetail' });
    params.setBusy('');
  }

  async function confirmClonePlan(plan: AccountClonePlan) {
    params.setBusy('执行克隆计划');
    await api<AccountClonePlan>(`/account-clone-plans/${plan.id}/confirm`, { method: 'POST' });
    params.showResult('克隆计划已执行', '已按克隆计划逐项执行，失败或需人工处理的项目可在账号详情中查看。');
    await refreshAccountDetail();
    params.setBusy('');
  }

  async function retryCloneItem(item: AccountCloneItem) {
    params.setBusy('重试克隆项');
    await api<AccountCloneItem>(`/account-clone-items/${item.id}/retry`, { method: 'POST' });
    params.showResult('克隆项已重试', '克隆项执行结果已刷新。');
    await refreshAccountDetail();
    params.setBusy('');
  }

  async function refreshRelatedDetails() {
    if (params.accountDetail) await refreshAccountDetail();
    if (params.accountPoolDetail) await refreshAccountPoolDetail();
    if (params.groupDetail) {
      const detail = await api<GroupDetail>(`/groups/${params.groupDetail.group.id}/detail`);
      params.setGroupDetail(detail);
    }
  }

  async function confirmVerificationTask(task: VerificationTask) {
    params.setBusy('处理验证辅助');
    try {
      const updated = await api<VerificationTask>(`/verification-tasks/${task.id}/confirm-action`, {
        method: 'POST',
        body: JSON.stringify({ actor: '普通用户' }),
      });
      if (updated.status === '失败') {
        params.showResult('验证辅助处理失败', updated.failure_detail || `${updated.verification_type}：失败`);
      } else if (updated.status === '需人工处理') {
        params.showResult('仍需人工处理', updated.failure_detail || updated.detected_reason || updated.verification_type);
      } else {
        params.showResult('验证辅助已处理', `${updated.verification_type}：${updated.status}`);
      }
      await refreshRelatedDetails();
    } catch (error) {
      params.handleActionError(error);
    } finally {
      params.setBusy('');
    }
  }

  async function resolveGroupRestrictionTask(task: VerificationTask) {
    params.setBusy('解除群限制重查');
    try {
      const updated = await api<VerificationTask>(`/verification-tasks/${task.id}/resolve-group-restriction`, {
        method: 'POST',
        body: JSON.stringify({ actor: '普通用户' }),
        timeoutMs: 8_000,
      });
      if (updated.status === '已处理') {
        params.showResult('群限制已解除', updated.failure_detail || `${updated.verification_type}：目标已可发言`);
      } else if (updated.status === '需人工处理') {
        params.showResult('仍需管理员处理', updated.failure_detail || '当前账号在该群仍不可发言。');
      } else {
        params.showResult('解除群限制重查失败', updated.failure_detail || `${updated.verification_type}：${updated.status}`);
      }
      await refreshRelatedDetails();
    } catch (error) {
      params.handleActionError(error);
    } finally {
      params.setBusy('');
    }
  }

  async function resolveGroupRestrictionBatch(task: VerificationTask) {
    params.setBusy('批量重查群限制');
    try {
      const result = await api<GroupRestrictionBatchResult>(`/verification-tasks/${task.id}/resolve-group-restriction-batch`, {
        method: 'POST',
        body: JSON.stringify({ actor: '普通用户' }),
        timeoutMs: 180_000,
      });
      params.showResult('目标账号重查完成', result.message);
      await refreshRelatedDetails();
    } catch (error) {
      params.handleActionError(error);
    } finally {
      params.setBusy('');
    }
  }

  async function dismissVerificationTask(task: VerificationTask) {
    params.setBusy('忽略验证辅助');
    try {
      await api<VerificationTask>(`/verification-tasks/${task.id}/dismiss`, { method: 'POST' });
      params.showResult('验证辅助已忽略', '该验证事项已从待处理列表移除。');
      if (params.accountDetail) await refreshAccountDetail();
      if (params.accountPoolDetail) await refreshAccountPoolDetail();
    } catch (error) {
      params.handleActionError(error);
    } finally {
      params.setBusy('');
    }
  }

  async function refreshAccountDetail() {
    if (!params.accountDetail) return;
    const detail = await api<AccountDetail>(`/tg-accounts/${params.accountDetail.account.id}/detail`);
    params.setAccountDetail(detail);
  }

  async function syncAccountContacts() {
    if (!params.accountDetail) return;
    params.setBusy('同步联系人');
    await api<Contact[]>(`/tg-accounts/${params.accountDetail.account.id}/contacts/sync`, { method: 'POST' });
    await refreshAccountDetail();
    params.showResult('联系人已同步', '已刷新联系人和群友候选，可以直接选中对象创建平台发送任务。');
    params.setBusy('');
  }

  async function queueAccountSyncNow() {
    if (!params.accountDetail) return;
    params.setBusy('同步账号数据');
    await api<AccountSyncRecord[]>(`/tg-accounts/${params.accountDetail.account.id}/sync-now`, { method: 'POST' });
    await refreshAccountDetail();
    await params.refresh();
    params.showResult('同步完成', '已同步资料、健康、群聊、云联系人和 TG 官方验证码。');
    params.setBusy('');
  }

  async function openGroupDetail(group: Group) {
    params.setBusy('读取群详情');
    const detail = await api<GroupDetail>(`/groups/${group.id}/detail`);
    params.setGroupDetail(detail);
    params.setSelectedGroupId(group.id);
    params.setModal({ type: 'groupDetail' });
    params.setBusy('');
  }

  function avatarUrl(value: string) {
    if (!value) return '';
    return value.startsWith('http') ? value : `${API_ORIGIN}${value}`;
  }

  function openAccountProfileEdit() {
    if (!params.accountDetail) return;
    params.setProfileForm({
      display_name: params.accountDetail.account.display_name,
      tg_first_name: params.accountDetail.account.tg_first_name || '',
      tg_last_name: params.accountDetail.account.tg_last_name || '',
      tg_bio: params.accountDetail.account.tg_bio || '',
      avatar_object_key: params.accountDetail.account.avatar_object_key || '',
    });
    params.setAvatarFile(null);
    params.setModal({ type: 'accountProfileEdit' });
  }

  async function pollVerificationCodes(reason: string) {
    if (!params.accountDetail) return;
    const accountId = params.accountDetail.account.id;
    const cleanReason = reason.trim();
    if (!cleanReason) {
      params.showResult('需要操作原因', '查看或同步 TG 官方验证码前必须填写原因。');
      return;
    }
    params.setBusy('同步验证码');
    try {
      const codes = await api<VerificationCode[]>(`/tg-accounts/${accountId}/verification-codes/poll`, { method: 'POST', body: JSON.stringify({ reason: cleanReason }) });
      params.setAccountDetail((current) => current?.account.id === accountId ? { ...current, verification_codes: codes } : current);
      params.showResult('验证码已同步', '已从 TG 官方服务消息同步最新验证码，验证码会短时展示并写入审计。');
    } finally {
      params.setBusy('');
    }
  }

  async function saveAccountProfile() {
    if (!params.accountDetail) return;
    params.setBusy('保存账号资料');
    let avatarObjectKey = params.profileForm.avatar_object_key;
    if (params.avatarFile) {
      const form = new FormData();
      form.append('file', params.avatarFile);
      const uploaded = await api<{ object_key: string; preview_url: string }>(`/tg-accounts/${params.accountDetail.account.id}/avatar`, {
        method: 'POST',
        body: form,
      });
      avatarObjectKey = uploaded.object_key;
    }
    await api<Account>(`/tg-accounts/${params.accountDetail.account.id}/profile`, {
      method: 'PATCH',
      body: JSON.stringify({ ...params.profileForm, avatar_object_key: avatarObjectKey }),
    });
    params.closeModal();
    params.showResult('账号资料已保存', '资料已进入后台同步处理，可在账号详情中查看同步状态。');
    await params.refresh();
    const detail = await api<AccountDetail>(`/tg-accounts/${params.accountDetail.account.id}/detail`);
    params.setAccountDetail(detail);
    params.setAccountDetailTab('资料');
    params.setModal({ type: 'accountDetail' });
    params.setBusy('');
  }

  async function retryAccountProfileSync() {
    if (!params.accountDetail) return;
    params.setBusy('重试资料同步');
    await api<ProfileSyncRecord>(`/tg-accounts/${params.accountDetail.account.id}/profile-sync/retry`, { method: 'POST' });
    params.showResult('已重新提交', '账号资料同步已重新提交处理。');
    await params.refresh();
    await refreshAccountDetail();
    params.setBusy('');
  }

  async function runLogin(account: Account, method: 'code' | 'qr') {
    await startOrResumeAccountLogin(account, method, method === 'code');
  }

  async function verifyAccount(account: Account) {
    params.setModal({ type: 'accountLogin' });
    params.setAccountLoginForm({
      ...EMPTY_ACCOUNT_LOGIN_FORM,
      account,
      method: account.status === '等待扫码' ? 'qr' : 'code',
      step: account.status === '等待2FA' ? 'password' : 'method',
    });
  }

  async function chooseAccountLoginMethod(method: 'code' | 'qr') {
    if (!params.accountLoginForm.account) return;
    await startOrResumeAccountLogin(params.accountLoginForm.account, method, false);
  }

  async function submitAccountLoginCode() {
    if (!params.accountLoginForm.account || !params.accountLoginForm.code.trim()) return;
    const account = params.accountLoginForm.account;
    const accountId = account.id;
    const code = params.accountLoginForm.code.trim();
    await params.runWithLoading(`account-login:${accountId}:code`, '验证登录', async () => {
      const updated = await api<Account>(`/tg-accounts/${account.id}/login/verify`, {
        method: 'POST',
        body: JSON.stringify({ code }),
      });
      await completeAccountLogin(updated);
    }).catch((error) => {
      params.setAccountLoginForm((current) => ({ ...current, error: params.errorMessage(error) }));
    });
  }

  async function submitAccountLoginPassword() {
    if (!params.accountLoginForm.account || !params.accountLoginForm.password_2fa) return;
    const account = params.accountLoginForm.account;
    const accountId = account.id;
    const password_2fa = params.accountLoginForm.password_2fa;
    await params.runWithLoading(`account-login:${accountId}:password`, '验证二步密码', async () => {
      const updated = await api<Account>(`/tg-accounts/${account.id}/login/verify`, {
        method: 'POST',
        body: JSON.stringify({ password_2fa }),
      });
      await completeAccountLogin(updated);
    }).catch((error) => {
      params.setAccountLoginForm((current) => ({ ...current, error: params.errorMessage(error) }));
    });
  }

  async function resendAccountLoginCode() {
    if (!params.accountLoginForm.account) return;
    await startOrResumeAccountLogin(params.accountLoginForm.account, 'code', true);
  }

  async function checkAccountQrLogin() {
    if (!params.accountLoginForm.account) return;
    params.setBusy('检查扫码结果');
    try {
      const updated = await api<Account>(`/tg-accounts/${params.accountLoginForm.account.id}/login/qr/check`, { method: 'POST' });
      await completeAccountLogin(updated);
    } catch (error) {
      params.setAccountLoginForm((current) => ({ ...current, error: params.errorMessage(error) }));
    } finally {
      params.setBusy('');
    }
  }

  async function healthCheck(account: Account) {
    params.setBusy('健康检查');
    const result = await api<Account>(`/tg-accounts/${account.id}/health-check`, { method: 'POST' });
    params.showResult('健康检查完成', `${account.display_name}：${result.status}，健康分 ${result.health_score}`);
    await params.refresh();
    if (params.accountDetail?.account.id === account.id) await refreshAccountDetail();
    if (params.accountPoolDetail) await refreshAccountPoolDetail();
  }

  async function syncAccountGroups(account: Account) {
    params.setBusy('同步账号数据');
    await api<AccountSyncRecord[]>(`/tg-accounts/${account.id}/sync-now`, { method: 'POST' });
    await api(`/tg-accounts/${account.id}/sync-targets`, { method: 'POST' }).catch(() => undefined);
    params.showResult('同步完成', `${account.display_name} 已同步资料、健康、群/频道目标、云联系人和验证码。`);
    await params.refresh();
    if (params.accountDetail?.account.id === account.id) await refreshAccountDetail();
    if (params.accountPoolDetail) await refreshAccountPoolDetail();
  }

  return {
    openAccountCreate,
    openAccountDetail,
    openAccountVerificationCodes,
    openAccountMovePool,
    openAccountPoolDetail,
    refreshAccountPoolDetail,
    createAccount,
    createAccountPool,
    moveCurrentAccountPool,
    createClonePlan,
    confirmClonePlan,
    retryCloneItem,
    confirmVerificationTask,
    resolveGroupRestrictionTask,
    resolveGroupRestrictionBatch,
    dismissVerificationTask,
    refreshAccountDetail,
    syncAccountContacts,
    queueAccountSyncNow,
    openGroupDetail,
    avatarUrl,
    openAccountProfileEdit,
    pollVerificationCodes,
    saveAccountProfile,
    retryAccountProfileSync,
    runLogin,
    verifyAccount,
    chooseAccountLoginMethod,
    submitAccountLoginCode,
    submitAccountLoginPassword,
    resendAccountLoginCode,
    checkAccountQrLogin,
    deleteAccount,
    healthCheck,
    syncAccountGroups,
  };
}
