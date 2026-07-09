import React from 'react';
import { Alert, Button, Card, Form, Input, Space, Tag, Typography, message } from 'antd';
import { LockKeyhole } from 'lucide-react';
import type { Tenant, TenantFixedTwoFaSettings } from '../types';

type FixedTwoFaForm = {
  password: string;
  reason: string;
};

type Props = {
  tenants: Tenant[];
  settings: Record<number, TenantFixedTwoFaSettings>;
  canManage: boolean;
  isActionPending: (key: string) => boolean;
  onSave: (tenantId: number, payload: FixedTwoFaForm) => Promise<boolean>;
};

export default function TenantFixedTwoFaSettingsView({ tenants, settings, canManage, isActionPending, onSave }: Props) {
  const [forms, setForms] = React.useState<Record<number, FixedTwoFaForm>>({});

  function tenantForm(tenantId: number) {
    return forms[tenantId] ?? { password: '', reason: '' };
  }

  function updateTenantForm(tenantId: number, patch: Partial<FixedTwoFaForm>) {
    setForms((current) => ({ ...current, [tenantId]: { ...tenantForm(tenantId), ...patch } }));
  }

  async function submitTenantForm(tenantId: number) {
    const payload = tenantForm(tenantId);
    if (!payload.password.trim() || !payload.reason.trim()) {
      void message.warning('请填写固定 2FA 密码和操作原因');
      return;
    }
    const saved = await onSave(tenantId, { password: payload.password.trim(), reason: payload.reason.trim() });
    if (!saved) return;
    setForms((current) => ({ ...current, [tenantId]: { password: '', reason: '' } }));
  }

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Alert
        type="warning"
        showIcon
        message="固定 2FA 密码"
        description="用于新登录后的普通账号和账号安全加固设置二步验证；已配置后不能修改，密码不会回显。"
      />
      {tenants.map((tenant) => {
        const setting = settings[tenant.id];
        const configured = Boolean(setting?.fixed_two_fa_password_configured);
        const form = tenantForm(tenant.id);
        return (
          <Card key={tenant.id} className="panel" title={tenant.name} extra={<Tag color={configured ? 'green' : 'orange'}>{configured ? '已配置' : '未配置'}</Tag>}>
            <Form layout="vertical">
              <Form.Item label="固定 2FA 密码">
                <Input.Password
                  value={form.password}
                  disabled={configured || !canManage}
                  placeholder={configured ? '已配置后不能修改' : '输入固定 2FA 密码'}
                  onChange={(event) => updateTenantForm(tenant.id, { password: event.target.value })}
                />
              </Form.Item>
              <Form.Item label="操作原因">
                <Input.TextArea
                  rows={2}
                  value={form.reason}
                  disabled={configured || !canManage}
                  placeholder={configured ? '已配置后不能修改' : '说明首次配置原因'}
                  onChange={(event) => updateTenantForm(tenant.id, { reason: event.target.value })}
                />
              </Form.Item>
              <Space>
                <Button
                  type="primary"
                  icon={<LockKeyhole size={16} />}
                  disabled={configured || !canManage}
                  loading={isActionPending(`tenant-fixed-2fa:${tenant.id}`)}
                  onClick={() => void submitTenantForm(tenant.id)}
                >
                  设置固定密码
                </Button>
                {setting?.fixed_two_fa_password_set_at && (
                  <Typography.Text type="secondary">
                    设置时间：{setting.fixed_two_fa_password_set_at.replace('T', ' ').slice(0, 16)}
                  </Typography.Text>
                )}
              </Space>
            </Form>
          </Card>
        );
      })}
    </Space>
  );
}
