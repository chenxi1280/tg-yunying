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
  VerificationChallengeContext,
  VerificationCode,
  VerificationTask,
} from '../types';

const GROUP_RESTRICTION_BATCH_TIMEOUT_MS = 300_000;

interface AccountActionParams {
  accountCreateForm: ReturnType<typeof defaultAccountCreateForm>;
  accountDetail: AccountDetail | null;
  accountDetailRequestRef: { current: { accountId: number | null; seq: number } };
  accountProfileSaveRequestRef: { current: { seq: number; accountId: number | null; signature: string } };
  accountDetailActionRequestRef: { current: { seq: number; accountId: number | null; action: string } };
  accountLoginRequestRef: { current: { seq: number; accountId: number | null; action: string } };
  accountPoolDetailRequestRef: { current: { poolId: number | null; seq: number } };
  accountLoginForm: AccountLoginForm;
  accountPoolDetail: AccountPoolDetail | null;
  accountPoolForm: ReturnType<typeof defaultAccountPoolForm>;
  accountPools: AccountPool[];
  avatarFile: File | null;
  cloneForm: { target_account_ids: number[]; clone_contacts: boolean; clone_groups: boolean };
  currentUser: CurrentUser | null;
  groupDetail: GroupDetail | null;
  groupDetailRequestRef: { current: { groupId: number | null; seq: number } };
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

interface AccountLoginRequest {
  readonly accountId: number;
  readonly action: string;
  readonly requestSeq: number;
}

export function createAccountActions(params: AccountActionParams) {
  function beginAccountDetailRequest(accountId: number) {
    const nextSeq = params.accountDetailRequestRef.current.seq + 1;
    params.accountDetailRequestRef.current = { accountId, seq: nextSeq };
    return nextSeq;
  }

  function isActiveAccountDetailRequest(accountId: number, seq: number) {
    const current = params.accountDetailRequestRef.current;
    return current.accountId === accountId && current.seq === seq;
  }

  function clearAccountDetailRequest(accountId: number, seq: number) {
    if (isActiveAccountDetailRequest(accountId, seq)) {
      params.accountDetailRequestRef.current = { accountId: null, seq };
    }
  }

  function beginAccountPoolDetailRequest(poolId: number) {
    const nextSeq = params.accountPoolDetailRequestRef.current.seq + 1;
    params.accountPoolDetailRequestRef.current = { poolId, seq: nextSeq };
    return nextSeq;
  }

  function isActiveAccountPoolDetailRequest(poolId: number, seq: number) {
    const current = params.accountPoolDetailRequestRef.current;
    return current.poolId === poolId && current.seq === seq;
  }

  function clearAccountPoolDetailRequest(poolId: number, seq: number) {
    if (isActiveAccountPoolDetailRequest(poolId, seq)) {
      params.accountPoolDetailRequestRef.current = { poolId: null, seq };
    }
  }

  function beginGroupDetailRequest(groupId: number) {
    const nextSeq = params.groupDetailRequestRef.current.seq + 1;
    params.groupDetailRequestRef.current = { groupId, seq: nextSeq };
    return nextSeq;
  }

  function isActiveGroupDetailRequest(groupId: number, seq: number) {
    const current = params.groupDetailRequestRef.current;
    return current.groupId === groupId && current.seq === seq;
  }

  function clearGroupDetailRequest(groupId: number, seq: number) {
    if (isActiveGroupDetailRequest(groupId, seq)) {
      params.groupDetailRequestRef.current = { groupId: null, seq };
    }
  }

  function accountProfilePayload() {
    return { ...params.profileForm };
  }

  function accountAvatarFileSignature(file: File | null) {
    if (!file) return null;
    return {
      name: file.name,
      size: file.size,
      type: file.type,
      lastModified: file.lastModified,
    };
  }

  function accountProfileSaveSignature(accountId: number, payload: ReturnType<typeof accountProfilePayload>, avatarFile: File | null) {
    return JSON.stringify({ accountId, payload, avatarFile: accountAvatarFileSignature(avatarFile) });
  }

  function beginAccountProfileSaveRequest(accountId: number, signature: string) {
    const requestSeq = params.accountProfileSaveRequestRef.current.seq + 1;
    params.accountProfileSaveRequestRef.current = { seq: requestSeq, accountId, signature };
    return requestSeq;
  }

  function isCurrentAccountProfileSaveRequest(requestSeq: number) {
    return params.accountProfileSaveRequestRef.current.seq === requestSeq;
  }

  function isActiveAccountProfileSaveRequest(accountId: number, requestSeq: number, signature: string) {
    return isCurrentAccountProfileSaveRequest(requestSeq)
      && params.accountProfileSaveRequestRef.current.accountId === accountId
      && params.accountProfileSaveRequestRef.current.signature === signature
      && params.accountDetail?.account.id === accountId
      && accountProfileSaveSignature(accountId, accountProfilePayload(), params.avatarFile) === signature;
  }

  function beginAccountDetailActionRequest(accountId: number, action: string) {
    const requestSeq = params.accountDetailActionRequestRef.current.seq + 1;
    params.accountDetailActionRequestRef.current = { seq: requestSeq, accountId, action };
    return requestSeq;
  }

  function isCurrentAccountDetailActionRequest(requestSeq: number) {
    return params.accountDetailActionRequestRef.current.seq === requestSeq;
  }

  function isActiveAccountDetailActionRequest(accountId: number, action: string, requestSeq: number) {
    return isCurrentAccountDetailActionRequest(requestSeq)
      && params.accountDetailActionRequestRef.current.accountId === accountId
      && params.accountDetailActionRequestRef.current.action === action
      && params.accountDetail?.account.id === accountId;
  }

  function beginAccountLoginRequest(accountId: number, action: string) {
    const requestSeq = params.accountLoginRequestRef.current.seq + 1;
    params.accountLoginRequestRef.current = { seq: requestSeq, accountId, action };
    return requestSeq;
  }

  function isCurrentAccountLoginRequest(requestSeq: number) {
    return params.accountLoginRequestRef.current.seq === requestSeq;
  }

  function isActiveAccountLoginRequest(request: AccountLoginRequest) {
    return isCurrentAccountLoginRequest(request.requestSeq)
      && params.accountLoginRequestRef.current.accountId === request.accountId
      && params.accountLoginRequestRef.current.action === request.action;
  }

  function setAccountLoginErrorIfActive(request: AccountLoginRequest, error: unknown) {
    if (!isActiveAccountLoginRequest(request)) return;
    params.setAccountLoginForm((current) => (
      current.account?.id === request.accountId
        ? { ...current, error: params.errorMessage(error) }
        : current
    ));
  }

  function updateAccountLoginFormIfActive(request: AccountLoginRequest, update: (current: AccountLoginForm) => AccountLoginForm) {
    if (!isActiveAccountLoginRequest(request)) return;
    params.setAccountLoginForm((current) => (
      current.account?.id === request.accountId ? update(current) : current
    ));
  }

  async function refreshAccountCenterDataAfterAction(actionLabel: string, refreshers: Array<() => Promise<void>>, shouldReport = () => true) {
    const errors: string[] = [];
    for (const refreshData of refreshers) {
      if (!shouldReport()) return;
      try {
        await refreshData();
      } catch (error) {
        errors.push(params.errorMessage(error));
      }
    }
    if (errors.length && shouldReport()) {
      params.showResult('账号中心数据刷新失败', `${actionLabel}操作已完成，但刷新账号中心数据失败：${errors[0]}`);
    }
  }

  async function refreshAccountListForAction() {
    await params.refresh();
  }

  async function refreshActionAccountDetail() {
    if (!params.accountDetail) return;
    const accountId = params.accountDetail.account.id;
    const requestSeq = beginAccountDetailRequest(accountId);
    try {
      const detail = await api<AccountDetail>(`/tg-accounts/${accountId}/detail`);
      if (!isActiveAccountDetailRequest(accountId, requestSeq)) return;
      params.setAccountDetail(detail);
    } catch (error) {
      if (!isActiveAccountDetailRequest(accountId, requestSeq)) return;
      throw error;
    } finally {
      clearAccountDetailRequest(accountId, requestSeq);
    }
  }

  async function refreshActionAccountPoolDetail() {
    if (!params.accountPoolDetail) return;
    const poolId = params.accountPoolDetail.pool.id;
    const requestSeq = beginAccountPoolDetailRequest(poolId);
    try {
      const detail = await api<AccountPoolDetail>(`/account-pools/${poolId}/detail`);
      if (!isActiveAccountPoolDetailRequest(poolId, requestSeq)) return;
      const selectedAccount = detail.accounts.find((account) => account.id === params.poolDirectAccountId);
      const defaultAccount = params.choosePoolSendAccount(detail);
      params.setAccountPoolDetail(detail);
      if (!selectedAccount || selectedAccount.status !== '在线') params.setPoolDirectAccountId(defaultAccount?.id || '');
    } catch (error) {
      if (!isActiveAccountPoolDetailRequest(poolId, requestSeq)) return;
      throw error;
    } finally {
      clearAccountPoolDetailRequest(poolId, requestSeq);
    }
  }

  async function refreshActionGroupDetail() {
    if (!params.groupDetail) return;
    const groupId = params.groupDetail.group.id;
    const requestSeq = beginGroupDetailRequest(groupId);
    try {
      const detail = await api<GroupDetail>(`/groups/${groupId}/detail`);
      if (!isActiveGroupDetailRequest(groupId, requestSeq)) return;
      params.setGroupDetail(detail);
    } catch (error) {
      if (!isActiveGroupDetailRequest(groupId, requestSeq)) return;
      throw error;
    } finally {
      clearGroupDetailRequest(groupId, requestSeq);
    }
  }

  function relatedDetailRefreshersForAction() {
    return [
      ...(params.accountDetail ? [refreshActionAccountDetail] : []),
      ...(params.accountPoolDetail ? [refreshActionAccountPoolDetail] : []),
      ...(params.groupDetail ? [refreshActionGroupDetail] : []),
    ];
  }

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

  async function openAccountDetail(account: Account): Promise<boolean> {
    const accountId = account.id;
    const requestSeq = beginAccountDetailRequest(accountId);
    params.setBusy('读取账号详情');
    try {
      const detail = await api<AccountDetail>(`/tg-accounts/${accountId}/detail`);
      if (!isActiveAccountDetailRequest(accountId, requestSeq)) return false;
      params.setAccountDetail(detail);
      params.setDirectMessageForm({ target_peer_id: '', target_display: '', content: '' });
      params.setAccountDetailTab('资料');
      params.setModal({ type: 'accountDetail' });
      return true;
    } catch (error) {
      if (!isActiveAccountDetailRequest(accountId, requestSeq)) return false;
      params.handleActionError(error);
      return false;
    } finally {
      if (isActiveAccountDetailRequest(accountId, requestSeq)) params.setBusy('');
      clearAccountDetailRequest(accountId, requestSeq);
    }
  }

  async function openAccountVerificationCodes(account: Account) {
    const accountId = account.id;
    const requestSeq = beginAccountDetailRequest(accountId);
    params.setBusy('提取验证码');
    try {
      const detail = await api<AccountDetail>(`/tg-accounts/${accountId}/detail`);
      if (!isActiveAccountDetailRequest(accountId, requestSeq)) return;
      params.setAccountDetail(detail);
      params.setDirectMessageForm({ target_peer_id: '', target_display: '', content: '' });
      params.setAccountDetailTab('TG 官方验证码');
      params.setModal({ type: 'accountDetail' });
      params.setNotice('请填写查看原因后同步提取 TG 官方验证码。');
    } catch (error) {
      if (!isActiveAccountDetailRequest(accountId, requestSeq)) return;
      params.handleActionError(error);
    } finally {
      if (isActiveAccountDetailRequest(accountId, requestSeq)) params.setBusy('');
      clearAccountDetailRequest(accountId, requestSeq);
    }
  }

  async function openAccountMovePool(account: Account) {
    const accountId = account.id;
    const requestSeq = beginAccountDetailRequest(accountId);
    params.setBusy('移动账号分组');
    try {
      const detail = await api<AccountDetail>(`/tg-accounts/${accountId}/detail`);
      if (!isActiveAccountDetailRequest(accountId, requestSeq)) return;
      params.setAccountDetail(detail);
      params.setModal({ type: 'accountMovePool' });
    } catch (error) {
      if (!isActiveAccountDetailRequest(accountId, requestSeq)) return;
      params.handleActionError(error);
    } finally {
      if (isActiveAccountDetailRequest(accountId, requestSeq)) params.setBusy('');
      clearAccountDetailRequest(accountId, requestSeq);
    }
  }

  async function openAccountPoolDetail(pool: AccountPool) {
    const poolId = pool.id;
    const requestSeq = beginAccountPoolDetailRequest(poolId);
    params.setBusy('读取账号分组详情');
    try {
      const detail = await api<AccountPoolDetail>(`/account-pools/${poolId}/detail`);
      if (!isActiveAccountPoolDetailRequest(poolId, requestSeq)) return;
      const defaultAccount = params.choosePoolSendAccount(detail);
      params.setAccountPoolDetail(detail);
      params.setPoolDirectAccountId(defaultAccount?.id || '');
      params.setDirectMessageForm({ target_peer_id: '', target_display: '', content: '' });
      params.setModal({ type: 'accountPoolDetail' });
    } catch (error) {
      if (!isActiveAccountPoolDetailRequest(poolId, requestSeq)) return;
      params.handleActionError(error);
    } finally {
      if (isActiveAccountPoolDetailRequest(poolId, requestSeq)) params.setBusy('');
      clearAccountPoolDetailRequest(poolId, requestSeq);
    }
  }

  async function refreshAccountPoolDetail() {
    if (!params.accountPoolDetail) return;
    const poolId = params.accountPoolDetail.pool.id;
    const requestSeq = beginAccountPoolDetailRequest(poolId);
    try {
      const detail = await api<AccountPoolDetail>(`/account-pools/${poolId}/detail`);
      if (!isActiveAccountPoolDetailRequest(poolId, requestSeq)) return;
      const selectedAccount = detail.accounts.find((account) => account.id === params.poolDirectAccountId);
      const defaultAccount = params.choosePoolSendAccount(detail);
      params.setAccountPoolDetail(detail);
      if (!selectedAccount || selectedAccount.status !== '在线') {
        params.setPoolDirectAccountId(defaultAccount?.id || '');
      }
    } catch (error) {
      if (!isActiveAccountPoolDetailRequest(poolId, requestSeq)) return;
      params.handleActionError(error);
    } finally {
      clearAccountPoolDetailRequest(poolId, requestSeq);
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
    const accountId = account.id;
    const action = `${resend ? 'resend' : method}`;
    const requestSeq = beginAccountLoginRequest(accountId, action);
    const request = { accountId, action, requestSeq };
    const isQr = method === 'qr';
    const actionKey = resend ? `account-login:${accountId}:resend` : `account-login:${accountId}:${method}`;
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
        const detail = await api<AccountDetail>(`/tg-accounts/${accountId}/detail`);
        if (!isActiveAccountLoginRequest(request)) return;
        flow = latestUsableCodeFlow(detail);
        if (params.accountDetail?.account.id === accountId) {
          params.setAccountDetail(detail);
        }
      }
      if (!flow && account.status !== '等待2FA') {
        flow = await api<LoginFlow>(`/tg-accounts/${accountId}/login/start`, {
          method: 'POST',
          body: JSON.stringify({ method }),
        });
        if (!isActiveAccountLoginRequest(request)) return;
      }
      const nextAccount = flow ? { ...account, status: flow.status } : account;
      updateAccountLoginFormIfActive(request, (current) => ({
        ...current,
        account: nextAccount,
        method,
        step: nextAccount.status === '等待2FA' ? 'password' : method === 'qr' || nextAccount.status === '等待扫码' ? 'qr' : 'code',
        flow,
        error: '',
      }));
      if (!isActiveAccountLoginRequest(request)) return;
      params.setNotice(isQr ? '请使用 Telegram 扫码确认登录。' : resend ? '已重新发送登录验证码。' : '请完成验证码登录。');
      await refreshAccountCenterDataAfterAction(resend ? '重新发送验证码' : isQr ? '扫码登录启动' : '验证码登录启动', [
        refreshAccountListForAction,
        ...(params.accountDetail?.account.id === accountId ? [refreshActionAccountDetail] : []),
      ], () => isActiveAccountLoginRequest(request));
    }).catch((error) => {
      setAccountLoginErrorIfActive(request, error);
    });
  }

  async function completeAccountLogin(updated: Account, request: AccountLoginRequest) {
    if (!isActiveAccountLoginRequest(request)) return;
    updateAccountLoginFormIfActive(request, (current) => ({ ...current, account: updated, error: '' }));
    await refreshAccountCenterDataAfterAction('登录状态推进', [
      refreshAccountListForAction,
      ...(params.accountDetail?.account.id === updated.id ? [refreshActionAccountDetail] : []),
    ], () => isActiveAccountLoginRequest(request));
    if (!isActiveAccountLoginRequest(request)) return;
    if (updated.status === '等待2FA') {
      updateAccountLoginFormIfActive(request, (current) => ({ ...current, account: updated, step: 'password', code: '', error: '' }));
      params.setNotice('验证码已通过，请输入 Telegram 二步验证密码。');
      return;
    }
    if (updated.status !== '在线') {
      updateAccountLoginFormIfActive(request, (current) => ({ ...current, account: updated, error: `登录未完成，当前状态：${updated.status}` }));
      return;
    }
    params.setAccountLoginForm((current) => current.account?.id === request.accountId ? EMPTY_ACCOUNT_LOGIN_FORM : current);
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
      await refreshAccountCenterDataAfterAction('账号新增', [refreshAccountListForAction]);
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
      await refreshAccountCenterDataAfterAction('账号删除', [
        refreshAccountListForAction,
        ...(params.accountPoolDetail ? [refreshActionAccountPoolDetail] : []),
      ]);
      params.setNotice(`${removed.display_name} 已移除，历史任务和归档记录仍会保留。`);
    } catch (error) {
      params.handleActionError(error);
    } finally {
      params.setBusy('');
    }
  }

  async function createAccountPool() {
    params.setBusy('新增账号分组');
    try {
      const pool = await api<AccountPool>('/account-pools', {
        method: 'POST',
        body: JSON.stringify({ tenant_id: params.currentUser?.tenant_id ?? 1, ...params.accountPoolForm }),
      });
      params.closeModal();
      params.showResult('账号分组已新增', `已新增账号分组：${pool.name}`);
      params.setAccountPoolForm({ name: '新账号分组', description: '', is_default: false });
      params.setSelectedPoolId(pool.id);
      await refreshAccountCenterDataAfterAction('账号分组新增', [refreshAccountListForAction]);
    } catch (error) {
      params.handleActionError(error);
    } finally {
      params.setBusy('');
    }
  }

  async function moveCurrentAccountPool(poolId: number) {
    if (!params.accountDetail) return;
    const accountId = params.accountDetail.account.id;
    const action = 'move-pool';
    const requestSeq = beginAccountDetailActionRequest(accountId, action);
    params.setBusy('移动账号分组');
    try {
      const updated = await api<Account>(`/tg-accounts/${accountId}/move-pool`, {
        method: 'POST',
        body: JSON.stringify({ pool_id: poolId }),
      });
      if (!isActiveAccountDetailActionRequest(accountId, action, requestSeq)) return;
      params.showResult('账号分组已更新', `${updated.display_name} 已移动到 ${updated.pool_name}`);
      await refreshAccountCenterDataAfterAction('账号分组移动', [refreshAccountListForAction, refreshActionAccountDetail]);
      if (!isActiveAccountDetailActionRequest(accountId, action, requestSeq)) return;
      params.setModal({ type: 'accountDetail' });
    } catch (error) {
      if (!isActiveAccountDetailActionRequest(accountId, action, requestSeq)) return;
      params.handleActionError(error);
    } finally {
      if (isCurrentAccountDetailActionRequest(requestSeq)) params.setBusy('');
    }
  }

  async function createClonePlan() {
    if (!params.accountDetail || !params.cloneForm.target_account_ids.length) return;
    const accountId = params.accountDetail.account.id;
    const action = 'clone-create';
    const requestSeq = beginAccountDetailActionRequest(accountId, action);
    params.setBusy('创建克隆计划');
    try {
      const clone_scope = [
        params.cloneForm.clone_contacts ? 'contacts' : '',
        params.cloneForm.clone_groups ? 'groups' : '',
      ].filter(Boolean);
      const plan = await api<AccountClonePlan>('/account-clone-plans', {
        method: 'POST',
        body: JSON.stringify({
          tenant_id: params.currentUser?.tenant_id ?? 1,
          source_account_id: accountId,
          target_account_ids: params.cloneForm.target_account_ids,
          clone_scope,
        }),
      });
      if (!isActiveAccountDetailActionRequest(accountId, action, requestSeq)) return;
      params.showResult('克隆计划已生成', `已生成 ${plan.items_total} 个克隆项，请确认后执行。`);
      await refreshAccountCenterDataAfterAction('克隆计划创建', [refreshActionAccountDetail]);
      if (!isActiveAccountDetailActionRequest(accountId, action, requestSeq)) return;
      params.setAccountDetailTab('克隆');
      params.setModal({ type: 'accountDetail' });
    } catch (error) {
      if (!isActiveAccountDetailActionRequest(accountId, action, requestSeq)) return;
      params.handleActionError(error);
    } finally {
      if (isCurrentAccountDetailActionRequest(requestSeq)) params.setBusy('');
    }
  }

  async function confirmClonePlan(plan: AccountClonePlan) {
    if (!params.accountDetail) return;
    const accountId = params.accountDetail.account.id;
    const action = `clone-confirm:${plan.id}`;
    const requestSeq = beginAccountDetailActionRequest(accountId, action);
    params.setBusy('执行克隆计划');
    try {
      await api<AccountClonePlan>(`/account-clone-plans/${plan.id}/confirm`, { method: 'POST' });
      if (!isActiveAccountDetailActionRequest(accountId, action, requestSeq)) return;
      params.showResult('克隆计划已执行', '已按克隆计划逐项执行，失败或需人工处理的项目可在账号详情中查看。');
      await refreshAccountCenterDataAfterAction('克隆计划执行', [refreshActionAccountDetail]);
    } catch (error) {
      if (!isActiveAccountDetailActionRequest(accountId, action, requestSeq)) return;
      params.handleActionError(error);
    } finally {
      if (isCurrentAccountDetailActionRequest(requestSeq)) params.setBusy('');
    }
  }

  async function retryCloneItem(item: AccountCloneItem) {
    if (!params.accountDetail) return;
    const accountId = params.accountDetail.account.id;
    const action = `clone-retry:${item.id}`;
    const requestSeq = beginAccountDetailActionRequest(accountId, action);
    params.setBusy('重试克隆项');
    try {
      await api<AccountCloneItem>(`/account-clone-items/${item.id}/retry`, { method: 'POST' });
      if (!isActiveAccountDetailActionRequest(accountId, action, requestSeq)) return;
      params.showResult('克隆项已重试', '克隆项执行结果已刷新。');
      await refreshAccountCenterDataAfterAction('克隆项重试', [refreshActionAccountDetail]);
    } catch (error) {
      if (!isActiveAccountDetailActionRequest(accountId, action, requestSeq)) return;
      params.handleActionError(error);
    } finally {
      if (isCurrentAccountDetailActionRequest(requestSeq)) params.setBusy('');
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
      await refreshAccountCenterDataAfterAction('验证辅助处理', relatedDetailRefreshersForAction());
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
      await refreshAccountCenterDataAfterAction('群限制重查', relatedDetailRefreshersForAction());
    } catch (error) {
      params.handleActionError(error);
    } finally {
      params.setBusy('');
    }
  }

  async function loadVerificationChallengeContext(task: VerificationTask) {
    return api<VerificationChallengeContext>(`/verification-tasks/${task.id}/challenge-context`);
  }

  async function refreshVerificationChallengeContext(task: VerificationTask) {
    return api<VerificationChallengeContext>(`/verification-tasks/${task.id}/refresh-challenge-context`, {
      method: 'POST',
      body: JSON.stringify({ actor: '普通用户' }),
      timeoutMs: 30_000,
    });
  }

  async function submitVerificationTaskResponse(task: VerificationTask, responseText: string) {
    params.setBusy('提交验证回复');
    try {
      const updated = await api<VerificationTask>(`/verification-tasks/${task.id}/submit-response`, {
        method: 'POST',
        body: JSON.stringify({ actor: '普通用户', response_text: responseText }),
        timeoutMs: 15_000,
      });
      if (updated.status === '已处理') {
        params.showResult('验证回复已提交', updated.failure_detail || '已恢复目标发言能力。');
      } else {
        params.showResult('仍需处理', updated.failure_detail || updated.detected_reason || '验证回复已提交，但目标仍不可发言。');
      }
      await refreshAccountCenterDataAfterAction('验证回复提交', relatedDetailRefreshersForAction());
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
        timeoutMs: GROUP_RESTRICTION_BATCH_TIMEOUT_MS,
      });
      params.showResult('目标账号重查完成', result.message);
      await refreshAccountCenterDataAfterAction('目标账号重查', relatedDetailRefreshersForAction());
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
      await refreshAccountCenterDataAfterAction('验证辅助忽略', [
        ...(params.accountDetail ? [refreshActionAccountDetail] : []),
        ...(params.accountPoolDetail ? [refreshActionAccountPoolDetail] : []),
      ]);
    } catch (error) {
      params.handleActionError(error);
    } finally {
      params.setBusy('');
    }
  }

  async function refreshAccountDetail() {
    if (!params.accountDetail) return;
    const accountId = params.accountDetail.account.id;
    const requestSeq = beginAccountDetailRequest(accountId);
    try {
      const detail = await api<AccountDetail>(`/tg-accounts/${accountId}/detail`);
      if (!isActiveAccountDetailRequest(accountId, requestSeq)) return;
      params.setAccountDetail(detail);
    } catch (error) {
      if (!isActiveAccountDetailRequest(accountId, requestSeq)) return;
      params.handleActionError(error);
    } finally {
      clearAccountDetailRequest(accountId, requestSeq);
    }
  }

  async function syncAccountContacts() {
    if (!params.accountDetail) return;
    const accountId = params.accountDetail.account.id;
    const action = 'sync-contacts';
    const requestSeq = beginAccountDetailActionRequest(accountId, action);
    params.setBusy('同步联系人');
    try {
      await api<Contact[]>(`/tg-accounts/${accountId}/contacts/sync`, { method: 'POST' });
      if (!isActiveAccountDetailActionRequest(accountId, action, requestSeq)) return;
      await refreshAccountCenterDataAfterAction('联系人同步', [refreshActionAccountDetail]);
      if (!isActiveAccountDetailActionRequest(accountId, action, requestSeq)) return;
      params.showResult('联系人已同步', '已刷新联系人和群友候选，可以直接选中对象创建平台发送任务。');
    } catch (error) {
      if (!isActiveAccountDetailActionRequest(accountId, action, requestSeq)) return;
      params.handleActionError(error);
    } finally {
      if (isCurrentAccountDetailActionRequest(requestSeq)) params.setBusy('');
    }
  }

  async function queueAccountSyncNow() {
    if (!params.accountDetail) return;
    const accountId = params.accountDetail.account.id;
    const action = 'sync-now';
    const requestSeq = beginAccountDetailActionRequest(accountId, action);
    params.setBusy('同步账号数据');
    try {
      await api<AccountSyncRecord[]>(`/tg-accounts/${accountId}/sync-now`, { method: 'POST' });
      if (!isActiveAccountDetailActionRequest(accountId, action, requestSeq)) return;
      await refreshAccountCenterDataAfterAction('账号数据同步', [refreshActionAccountDetail, refreshAccountListForAction]);
      if (!isActiveAccountDetailActionRequest(accountId, action, requestSeq)) return;
      params.showResult('同步完成', '已同步资料、健康、群聊、云联系人和 TG 官方验证码。');
    } catch (error) {
      if (!isActiveAccountDetailActionRequest(accountId, action, requestSeq)) return;
      params.handleActionError(error);
    } finally {
      if (isCurrentAccountDetailActionRequest(requestSeq)) params.setBusy('');
    }
  }

  async function openGroupDetail(group: Group): Promise<boolean> {
    const groupId = group.id;
    const requestSeq = beginGroupDetailRequest(groupId);
    params.setBusy('读取群详情');
    try {
      const detail = await api<GroupDetail>(`/groups/${groupId}/detail`);
      if (!isActiveGroupDetailRequest(groupId, requestSeq)) return false;
      params.setGroupDetail(detail);
      params.setSelectedGroupId(group.id);
      params.setModal({ type: 'groupDetail' });
      return true;
    } catch (error) {
      if (!isActiveGroupDetailRequest(groupId, requestSeq)) return false;
      params.handleActionError(error);
      return false;
    } finally {
      if (isActiveGroupDetailRequest(groupId, requestSeq)) params.setBusy('');
      clearGroupDetailRequest(groupId, requestSeq);
    }
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
    } catch (error) {
      params.handleActionError(error);
    } finally {
      params.setBusy('');
    }
  }

  async function saveAccountProfile() {
    if (!params.accountDetail) return;
    const accountId = params.accountDetail.account.id;
    const payload = accountProfilePayload();
    const avatarFile = params.avatarFile;
    const signature = accountProfileSaveSignature(accountId, payload, avatarFile);
    const requestSeq = beginAccountProfileSaveRequest(accountId, signature);
    params.setBusy('保存账号资料');
    try {
      let avatarObjectKey = payload.avatar_object_key;
      if (avatarFile) {
        const form = new FormData();
        form.append('file', avatarFile);
        const uploaded = await api<{ object_key: string; preview_url: string }>(`/tg-accounts/${accountId}/avatar`, {
          method: 'POST',
          body: form,
        });
        if (!isActiveAccountProfileSaveRequest(accountId, requestSeq, signature)) return;
        avatarObjectKey = uploaded.object_key;
      }
      await api<Account>(`/tg-accounts/${accountId}/profile`, {
        method: 'PATCH',
        body: JSON.stringify({ ...payload, avatar_object_key: avatarObjectKey }),
      });
      if (!isActiveAccountProfileSaveRequest(accountId, requestSeq, signature)) return;
      params.closeModal();
      params.showResult('账号资料已保存', '资料已进入后台同步处理，可在账号详情中查看同步状态。');
      await refreshAccountCenterDataAfterAction('账号资料保存', [refreshAccountListForAction, refreshActionAccountDetail]);
      params.setAccountDetailTab('资料');
      params.setModal({ type: 'accountDetail' });
    } catch (error) {
      if (!isActiveAccountProfileSaveRequest(accountId, requestSeq, signature)) return;
      params.handleActionError(error);
    } finally {
      if (isCurrentAccountProfileSaveRequest(requestSeq)) params.setBusy('');
    }
  }

  async function retryAccountProfileSync() {
    if (!params.accountDetail) return;
    const accountId = params.accountDetail.account.id;
    const action = 'profile-sync-retry';
    const requestSeq = beginAccountDetailActionRequest(accountId, action);
    params.setBusy('重试资料同步');
    try {
      await api<ProfileSyncRecord>(`/tg-accounts/${accountId}/profile-sync/retry`, { method: 'POST' });
      if (!isActiveAccountDetailActionRequest(accountId, action, requestSeq)) return;
      params.showResult('已重新提交', '账号资料同步已重新提交处理。');
      await refreshAccountCenterDataAfterAction('资料同步重试', [refreshAccountListForAction, refreshActionAccountDetail]);
    } catch (error) {
      if (!isActiveAccountDetailActionRequest(accountId, action, requestSeq)) return;
      params.handleActionError(error);
    } finally {
      if (isCurrentAccountDetailActionRequest(requestSeq)) params.setBusy('');
    }
  }

  async function runLogin(account: Account, method: 'code' | 'qr') {
    await startOrResumeAccountLogin(account, method, method === 'code');
  }

  async function verifyAccount(account: Account) {
    beginAccountLoginRequest(account.id, 'open');
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
    const action = 'code-submit';
    const requestSeq = beginAccountLoginRequest(accountId, action);
    const request = { accountId, action, requestSeq };
    const code = params.accountLoginForm.code.trim();
    await params.runWithLoading(`account-login:${accountId}:code`, '验证登录', async () => {
      const updated = await api<Account>(`/tg-accounts/${accountId}/login/verify`, {
        method: 'POST',
        body: JSON.stringify({ code }),
      });
      if (!isActiveAccountLoginRequest(request)) return;
      await completeAccountLogin(updated, request);
    }).catch((error) => {
      setAccountLoginErrorIfActive(request, error);
    });
  }

  async function submitAccountLoginPassword() {
    if (!params.accountLoginForm.account || !params.accountLoginForm.password_2fa) return;
    const account = params.accountLoginForm.account;
    const accountId = account.id;
    const action = 'password-submit';
    const requestSeq = beginAccountLoginRequest(accountId, action);
    const request = { accountId, action, requestSeq };
    const password_2fa = params.accountLoginForm.password_2fa;
    await params.runWithLoading(`account-login:${accountId}:password`, '验证二步密码', async () => {
      const updated = await api<Account>(`/tg-accounts/${accountId}/login/verify`, {
        method: 'POST',
        body: JSON.stringify({ password_2fa }),
      });
      if (!isActiveAccountLoginRequest(request)) return;
      await completeAccountLogin(updated, request);
    }).catch((error) => {
      setAccountLoginErrorIfActive(request, error);
    });
  }

  async function resendAccountLoginCode() {
    if (!params.accountLoginForm.account) return;
    await startOrResumeAccountLogin(params.accountLoginForm.account, 'code', true);
  }

  async function checkAccountQrLogin() {
    if (!params.accountLoginForm.account) return;
    const accountId = params.accountLoginForm.account.id;
    const action = 'qr-check';
    const requestSeq = beginAccountLoginRequest(accountId, action);
    const request = { accountId, action, requestSeq };
    params.setBusy('检查扫码结果');
    try {
      const updated = await api<Account>(`/tg-accounts/${accountId}/login/qr/check`, { method: 'POST' });
      if (!isActiveAccountLoginRequest(request)) return;
      await completeAccountLogin(updated, request);
    } catch (error) {
      setAccountLoginErrorIfActive(request, error);
    } finally {
      if (isCurrentAccountLoginRequest(requestSeq)) params.setBusy('');
    }
  }

  async function healthCheck(account: Account) {
    params.setBusy('健康检查');
    try {
      const result = await api<Account>(`/tg-accounts/${account.id}/health-check`, { method: 'POST' });
      params.showResult('健康检查完成', `${account.display_name}：${result.status}，健康分 ${result.health_score}`);
      await refreshAccountCenterDataAfterAction('健康检查', [
        refreshAccountListForAction,
        ...(params.accountDetail?.account.id === account.id ? [refreshActionAccountDetail] : []),
        ...(params.accountPoolDetail ? [refreshActionAccountPoolDetail] : []),
      ]);
    } catch (error) {
      params.handleActionError(error);
    } finally {
      params.setBusy('');
    }
  }

  async function syncAccountGroups(account: Account) {
    params.setBusy('同步账号数据');
    try {
      await api<AccountSyncRecord[]>(`/tg-accounts/${account.id}/sync-now`, { method: 'POST' });
      await api(`/tg-accounts/${account.id}/sync-targets`, { method: 'POST' });
      params.showResult('同步完成', `${account.display_name} 已同步资料、健康、群/频道目标、云联系人和验证码。`);
      await refreshAccountCenterDataAfterAction('账号同步', [
        refreshAccountListForAction,
        ...(params.accountDetail?.account.id === account.id ? [refreshActionAccountDetail] : []),
        ...(params.accountPoolDetail ? [refreshActionAccountPoolDetail] : []),
      ]);
    } catch (error) {
      params.handleActionError(error);
    } finally {
      params.setBusy('');
    }
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
    loadVerificationChallengeContext,
    refreshVerificationChallengeContext,
    resolveGroupRestrictionTask,
    resolveGroupRestrictionBatch,
    submitVerificationTaskResponse,
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
