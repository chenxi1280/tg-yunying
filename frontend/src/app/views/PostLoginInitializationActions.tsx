import React from 'react';
import { Button, Input, Modal, Space } from 'antd';
import { api } from '../../shared/api/client';
import type { AccountPostLoginInitialization } from '../types';

type ActionKind = 'reconcile' | 'candidate' | 'email' | 'assume';

interface Props {
  detail: AccountPostLoginInitialization;
  loading: boolean;
  canManage: boolean;
  canManageTwoFa: boolean;
  onLoading: (value: boolean) => void;
  onError: (value: string) => void;
  onCompleted: (detail: AccountPostLoginInitialization) => Promise<void>;
}

export function PostLoginInitializationActions(props: Props) {
  const [action, setAction] = React.useState<ActionKind | null>(null);
  const [reason, setReason] = React.useState('');
  const [value, setValue] = React.useState('');
  const actions = availableActions(props.detail, props.canManage, props.canManageTwoFa);

  async function submit() {
    if (!action || !reason.trim()) return;
    props.onLoading(true);
    try {
      const result = await api<AccountPostLoginInitialization>(actionPath(props.detail.id, action), {
        method: 'POST',
        body: JSON.stringify(actionPayload(props.detail, action, reason, value)),
      });
      setAction(null);
      setReason('');
      setValue('');
      props.onError('');
      await props.onCompleted(result);
    } catch (requestError) {
      props.onError(requestError instanceof Error ? requestError.message : '后置初始化操作失败');
    } finally {
      setValue('');
      props.onLoading(false);
    }
  }

  return (
    <>
      {actions.length > 0 && (
        <Space style={{ marginTop: 16 }} wrap>
          {actions.map((item) => <Button key={item.kind} onClick={() => setAction(item.kind)}>{item.label}</Button>)}
        </Space>
      )}
      <Modal
        title={action ? actionLabel(action) : ''}
        open={action !== null}
        confirmLoading={props.loading}
        okButtonProps={{ disabled: !reason.trim() || needsValue(action) && !value.trim() }}
        onOk={() => void submit()}
        onCancel={() => { setAction(null); setReason(''); setValue(''); }}
      >
        {action === 'candidate' && <Input.Password value={value} onChange={(event) => setValue(event.target.value)} placeholder="当前 2FA 候选密码" autoComplete="new-password" />}
        {action === 'email' && <Input value={value} onChange={(event) => setValue(event.target.value)} placeholder="Telegram 恢复邮箱验证码" autoComplete="one-time-code" />}
        <Input value={reason} onChange={(event) => setReason(event.target.value)} placeholder="操作原因（必填）" style={{ marginTop: 12 }} />
      </Modal>
    </>
  );
}

function availableActions(detail: AccountPostLoginInitialization, canManage: boolean, canManageTwoFa: boolean) {
  const actions: Array<{ kind: ActionKind; label: string }> = [];
  if (recheckAllowed(detail) && canManage) actions.push({
    kind: 'reconcile',
    label: detail.status === 'reconcile_unknown' ? '对账原操作' : '重新检查后置阶段',
  });
  if (canManageTwoFa && candidateAllowed(detail)) actions.push({ kind: 'candidate', label: '提交当前 2FA' });
  if (canManageTwoFa && detail.failure_type.includes('email')) actions.push({ kind: 'email', label: '确认恢复邮箱' });
  if (canManage && ['manual_required', 'reconcile_unknown', 'waiting_abc_approval'].includes(detail.status)) actions.push({ kind: 'assume', label: '接管执行' });
  return actions;
}

function recheckAllowed(detail: AccountPostLoginInitialization) {
  if (detail.status === 'reconcile_unknown') return true;
  if (!['failed', 'manual_required'].includes(detail.status)) return false;
  if (detail.failure_type === 'two_fa_source_resolution_failed') return true;
  if (['failed', 'manual_required'].includes(detail.profile_status)) return true;
  return ['failed', 'manual_required', 'reconcile_unknown'].includes(detail.abc_status);
}

function candidateAllowed(detail: AccountPostLoginInitialization) {
  return detail.status === 'manual_required' && [
    'two_fa_current_password_unavailable',
    'two_fa_remote_confirmed_no_effect',
    'two_fa_remote_effect_unproven',
    'two_fa_manual_required',
  ].includes(detail.failure_type);
}

function actionPath(id: number, action: ActionKind) {
  const suffix = {
    reconcile: 'reconcile',
    candidate: 'two-fa-current-candidate',
    email: 'two-fa-email-confirmation',
    assume: 'assume-execution-owner',
  }[action];
  return `/tg-accounts/post-login-initializations/${id}/${suffix}`;
}

function actionPayload(detail: AccountPostLoginInitialization, action: ActionKind, reason: string, value: string) {
  const payload: Record<string, string | number> = { expected_version: detail.version, reason: reason.trim() };
  if (action === 'candidate') payload.candidate_password = value;
  if (action === 'email') payload.confirmation_code = value;
  return payload;
}

function actionLabel(action: ActionKind) {
  return { reconcile: '对账原远端操作', candidate: '提交当前 2FA 候选', email: '确认 2FA 恢复邮箱', assume: '接管后置初始化' }[action];
}

function needsValue(action: ActionKind | null) {
  return action === 'candidate' || action === 'email';
}
