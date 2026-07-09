import React from 'react';
import { Alert, Button, Card, Input, Space, Typography, message } from 'antd';
import { api } from '../../shared/api/client';

type Managed2FaAction = 'save' | 'rotate' | 'reveal';

type Managed2FaRevealResponse = {
  account_id: number;
  password: string;
  revealed_at: string;
};

function managed2FaPath(accountId: number, action: Managed2FaAction) {
  const suffix = action === 'save' ? 'managed-2fa' : `managed-2fa/${action}`;
  return `/tg-accounts/${accountId}/security/${suffix}`;
}

export function AccountManaged2FaSettingsPanel({
  accountId,
  accountIdentity = 'normal',
  canManageCredentials,
}: {
  accountId: number;
  accountIdentity?: string;
  canManageCredentials: boolean;
}) {
  const [password, setPassword] = React.useState('');
  const [reason, setReason] = React.useState('');
  const [revealedPassword, setRevealedPassword] = React.useState('');
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState('');
  const activeAccountId = React.useRef(accountId);
  const managed2FaRequestRef = React.useRef({ accountId, action: '' as Managed2FaAction | '', seq: 0 });
  const latestManaged2FaPayloadSignature = React.useRef('');
  const managed2FaPayload = React.useMemo(() => ({
    password: password.trim(),
    reason: reason.trim(),
  }), [password, reason]);
  const managed2FaPayloadSignature = React.useMemo(() => JSON.stringify(managed2FaPayload), [managed2FaPayload]);
  const isCodeReceiver = accountIdentity === 'code_receiver';
  const canChangeManagedPassword = !isCodeReceiver && canManageCredentials;
  latestManaged2FaPayloadSignature.current = managed2FaPayloadSignature;

  React.useEffect(() => {
    activeAccountId.current = accountId;
    managed2FaRequestRef.current = { accountId, action: '', seq: managed2FaRequestRef.current.seq + 1 };
    setPassword('');
    setReason('');
    setRevealedPassword('');
    setError('');
    setLoading(false);
  }, [accountId]);

  function isActiveAccount(targetAccountId: number) {
    return activeAccountId.current === targetAccountId;
  }

  function beginManaged2FaRequest(targetAccountId: number, action: Managed2FaAction) {
    const requestSeq = managed2FaRequestRef.current.seq + 1;
    managed2FaRequestRef.current = { accountId: targetAccountId, action, seq: requestSeq };
    return requestSeq;
  }

  function isCurrentManaged2FaRequest(targetAccountId: number, action: Managed2FaAction, requestSeq: number) {
    return isActiveAccount(targetAccountId)
      && managed2FaRequestRef.current.accountId === targetAccountId
      && managed2FaRequestRef.current.action === action
      && managed2FaRequestRef.current.seq === requestSeq;
  }

  function isActiveManaged2FaRequest(targetAccountId: number, action: Managed2FaAction, requestSeq: number, payloadSignature: string) {
    return isCurrentManaged2FaRequest(targetAccountId, action, requestSeq)
      && latestManaged2FaPayloadSignature.current === payloadSignature;
  }

  async function saveManagedPassword(action: Managed2FaAction) {
    const targetAccountId = accountId;
    const payload = managed2FaPayload;
    const payloadSignature = managed2FaPayloadSignature;
    const trimmedPassword = payload.password;
    const trimmedReason = payload.reason;
    if (!trimmedPassword || !trimmedReason) {
      setError('');
      void message.warning('请填写托管 2FA 密码和操作原因');
      return;
    }
    const requestSeq = beginManaged2FaRequest(targetAccountId, action);
    setLoading(true);
    setError('');
    setRevealedPassword('');
    try {
      const path = managed2FaPath(targetAccountId, action);
      await api(path, {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      if (!isActiveManaged2FaRequest(targetAccountId, action, requestSeq, payloadSignature)) return;
      setPassword('');
      setReason('');
      void message.success('平台托管 2FA 策略已提交');
    } catch (error) {
      if (!isActiveManaged2FaRequest(targetAccountId, action, requestSeq, payloadSignature)) return;
      setError(error instanceof Error ? error.message : '保存托管 2FA 失败');
    } finally {
      if (isCurrentManaged2FaRequest(targetAccountId, action, requestSeq)) setLoading(false);
    }
  }

  async function revealManagedPassword() {
    const targetAccountId = accountId;
    const requestSeq = beginManaged2FaRequest(targetAccountId, 'reveal');
    setLoading(true);
    setError('');
    setRevealedPassword('');
    try {
      const revealed = await api<Managed2FaRevealResponse>(managed2FaPath(targetAccountId, 'reveal'), {
        method: 'POST',
      });
      if (!isCurrentManaged2FaRequest(targetAccountId, 'reveal', requestSeq)) return;
      setRevealedPassword(revealed.password);
      void message.success('托管 2FA 密码已显示');
    } catch (error) {
      if (!isCurrentManaged2FaRequest(targetAccountId, 'reveal', requestSeq)) return;
      setError(error instanceof Error ? error.message : '查看托管 2FA 失败');
    } finally {
      if (isCurrentManaged2FaRequest(targetAccountId, 'reveal', requestSeq)) setLoading(false);
    }
  }

  async function copyRevealedPassword() {
    if (!revealedPassword) return;
    await navigator.clipboard.writeText(revealedPassword);
    void message.success('托管密码已复制');
  }

  return (
    <Card className="sub-panel compact-panel" title="托管 2FA">
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Alert
          type={isCodeReceiver ? 'info' : 'warning'}
          showIcon
          message={isCodeReceiver ? '接码专用账号只允许查看托管密码' : '密码设置 / 轮换不回显旧密码'}
          description={isCodeReceiver ? '接码专用账号禁止修改二步验证密码；查看和复制托管密码仍会写审计。' : '平台托管 2FA 用于备用 session 自动补齐。查看、导出、轮换和自动登录使用都必须写审计；未托管账号会在备用 session 补齐时显示阻塞原因。'}
        />
        {error && <Alert type="error" showIcon message={error} />}
        <Typography.Text type="secondary">权限：accounts.security.credential_manage</Typography.Text>
        {!isCodeReceiver && (
          <>
            <Input.Password
              disabled={!canChangeManagedPassword}
              value={password}
              placeholder="输入新的平台托管 2FA 密码"
              onChange={(event) => setPassword(event.target.value)}
            />
            <Input.TextArea
              disabled={!canChangeManagedPassword}
              rows={2}
              value={reason}
              placeholder="保存或轮换的操作原因"
              onChange={(event) => setReason(event.target.value)}
            />
          </>
        )}
        {revealedPassword && (
          <Input.Password
            readOnly
            value={revealedPassword}
            addonAfter={<Button type="link" size="small" onClick={() => void copyRevealedPassword()}>复制托管密码</Button>}
          />
        )}
        <Space wrap>
          {!isCodeReceiver && (
            <>
              <Button
                type="primary"
                disabled={!canChangeManagedPassword}
                loading={loading}
                onClick={() => saveManagedPassword('save')}
              >
                保存托管策略
              </Button>
              <Button
                disabled={!canChangeManagedPassword}
                loading={loading}
                onClick={() => saveManagedPassword('rotate')}
              >
                轮换托管密码
              </Button>
            </>
          )}
          <Button
            disabled={!canManageCredentials}
            loading={loading}
            onClick={() => void revealManagedPassword()}
          >
            查看托管密码
          </Button>
        </Space>
      </Space>
    </Card>
  );
}
