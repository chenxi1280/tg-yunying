import React from 'react';
import { Alert, Button, Card, Input, Space, Typography, message } from 'antd';
import { api } from '../../shared/api/client';

export function AccountManaged2FaSettingsPanel({
  accountId,
  canManageCredentials,
}: {
  accountId: number;
  canManageCredentials: boolean;
}) {
  const [password, setPassword] = React.useState('');
  const [reason, setReason] = React.useState('');
  const [loading, setLoading] = React.useState(false);

  async function saveManagedPassword(path: string) {
    const trimmedPassword = password.trim();
    const trimmedReason = reason.trim();
    if (!trimmedPassword || !trimmedReason) {
      void message.warning('请填写托管 2FA 密码和操作原因');
      return;
    }
    setLoading(true);
    try {
      await api(path, {
        method: 'POST',
        body: JSON.stringify({ password: trimmedPassword, reason: trimmedReason }),
      });
      setPassword('');
      setReason('');
      void message.success('平台托管 2FA 策略已提交');
    } finally {
      setLoading(false);
    }
  }

  return (
    <Card className="sub-panel compact-panel" title="托管 2FA">
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Alert
          type="warning"
          showIcon
          message="密码设置 / 轮换不回显旧密码"
          description="平台托管 2FA 用于备用 session 自动补齐。查看、导出、轮换和自动登录使用都必须写审计；未托管账号会在备用 session 补齐时显示阻塞原因。"
        />
        <Typography.Text type="secondary">权限：accounts.security.credential_manage</Typography.Text>
        <Input.Password
          disabled={!canManageCredentials}
          value={password}
          placeholder="输入新的平台托管 2FA 密码"
          onChange={(event) => setPassword(event.target.value)}
        />
        <Input.TextArea
          disabled={!canManageCredentials}
          rows={2}
          value={reason}
          placeholder="操作原因"
          onChange={(event) => setReason(event.target.value)}
        />
        <Space wrap>
          <Button
            type="primary"
            disabled={!canManageCredentials}
            loading={loading}
            onClick={() => saveManagedPassword(`/tg-accounts/${accountId}/security/managed-2fa`)}
          >
            保存托管策略
          </Button>
          <Button
            disabled={!canManageCredentials}
            loading={loading}
            onClick={() => saveManagedPassword(`/tg-accounts/${accountId}/security/managed-2fa/rotate`)}
          >
            轮换托管密码
          </Button>
        </Space>
      </Space>
    </Card>
  );
}
