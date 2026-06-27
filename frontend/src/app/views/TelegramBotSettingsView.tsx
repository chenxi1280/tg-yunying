import React from 'react';
import { Alert, Button, Card, Checkbox, Descriptions, Input, Space, Tag, Typography } from 'antd';
import type { Tenant, TenantBotSettings } from '../types';
import { Badge } from '../components/shared';

type TenantBotSettingsPayload = {
  admin_chat_id: string;
  telegram_bot_token?: string;
  ai_group_bot_enabled: boolean;
  notify_ai_failures_enabled: boolean;
};

interface Props {
  tenants: Tenant[];
  botSettings: Record<number, TenantBotSettings>;
  canManageBotSettings: boolean;
  onSaveTenantBotSettings: (tenantId: number, payload: TenantBotSettingsPayload) => Promise<void>;
  onTestTenantBotMessage: (tenantId: number) => Promise<void>;
  onRefreshTenantBotWebhook: (tenantId: number) => Promise<void>;
  onDeleteTenantBotWebhook: (tenantId: number) => Promise<void>;
  isActionPending: (key: string) => boolean;
}

function TenantBotCard({ tenant, settings, canManage, isActionPending, onSave, onTest, onRefreshWebhook, onDeleteWebhook }: {
  tenant: Tenant;
  settings?: TenantBotSettings;
  canManage: boolean;
  isActionPending: Props['isActionPending'];
  onSave: Props['onSaveTenantBotSettings'];
  onTest: Props['onTestTenantBotMessage'];
  onRefreshWebhook: Props['onRefreshTenantBotWebhook'];
  onDeleteWebhook: Props['onDeleteTenantBotWebhook'];
}) {
  const [adminChatId, setAdminChatId] = React.useState(settings?.admin_chat_id || tenant.admin_chat_id || '');
  const [telegramBotToken, setTelegramBotToken] = React.useState('');
  const [aiGroupBotEnabled, setAiGroupBotEnabled] = React.useState(Boolean(settings?.ai_group_bot_enabled ?? tenant.ai_group_bot_enabled));
  const [notifyAiFailuresEnabled, setNotifyAiFailuresEnabled] = React.useState(Boolean(settings?.notify_ai_failures_enabled ?? tenant.notify_ai_failures_enabled));
  const configured = Boolean(settings?.telegram_bot_configured ?? tenant.telegram_bot_configured);
  const webhookStatus = settings?.telegram_bot_webhook_status || tenant.telegram_bot_webhook_status || 'not_configured';
  const saveDisabled = !canManage || !adminChatId.trim() || (!configured && !telegramBotToken.trim());

  React.useEffect(() => {
    setAdminChatId(settings?.admin_chat_id || tenant.admin_chat_id || '');
    setTelegramBotToken('');
    setAiGroupBotEnabled(Boolean(settings?.ai_group_bot_enabled ?? tenant.ai_group_bot_enabled));
    setNotifyAiFailuresEnabled(Boolean(settings?.notify_ai_failures_enabled ?? tenant.notify_ai_failures_enabled));
  }, [settings, tenant]);

  return (
    <Card className="developer-card status-accent neutral" size="small" title={tenant.name} extra={<Badge tone={configured ? 'positive' : 'neutral'}>{configured ? '已配置' : '未配置'}</Badge>}>
      <Space direction="vertical" size={10} style={{ width: '100%' }}>
        <Alert type="info" showIcon message="Bot Token 保存后不会明文回显；Admin Chat ID 支持多个，每行或逗号分隔；AI 活群 Bot 设置开关只控制 bot 内修改任务配置，不影响 Web 任务设置。" />
        <div className="policy-grid">
          <label>Admin Chat ID<Input.TextArea disabled={!canManage} autoSize={{ minRows: 2, maxRows: 5 }} value={adminChatId} onChange={(event) => setAdminChatId(event.target.value)} placeholder="每行或逗号分隔多个 Telegram 管理员 chat id" /></label>
          <label>Bot Token<Input.Password disabled={!canManage} value={telegramBotToken} onChange={(event) => setTelegramBotToken(event.target.value)} placeholder={configured ? '已配置，留空表示不更换' : '粘贴 Bot Token'} /></label>
          <Checkbox disabled={!canManage} checked={aiGroupBotEnabled} onChange={(event) => setAiGroupBotEnabled(event.target.checked)}>允许 TG bot 设置 AI 活群任务</Checkbox>
          <Checkbox disabled={!canManage} checked={notifyAiFailuresEnabled} onChange={(event) => setNotifyAiFailuresEnabled(event.target.checked)}>启用 AI 失败通知</Checkbox>
        </div>
        <Space wrap>
          <Tag color={webhookStatusColor(webhookStatus)}>Webhook：{webhookStatus}</Tag>
          <Typography.Text type="secondary">{settings?.telegram_bot_webhook_url || '保存后生成公网 webhook 地址'}</Typography.Text>
        </Space>
        <Descriptions size="small" column={1} bordered>
          <Descriptions.Item label="期望 URL">{settings?.telegram_bot_webhook_url || '-'}</Descriptions.Item>
          <Descriptions.Item label="Telegram 当前 URL">{settings?.telegram_bot_webhook_current_url || '-'}</Descriptions.Item>
          <Descriptions.Item label="最后检查">{settings?.telegram_bot_webhook_last_checked_at || '-'}</Descriptions.Item>
        </Descriptions>
        {settings?.telegram_bot_last_error && <Alert type="error" showIcon message={settings.telegram_bot_last_error} />}
        <Space>
          <Button
            type="primary"
            disabled={saveDisabled}
            loading={isActionPending(`tenant:${tenant.id}:bot:save`)}
            onClick={() => onSave(tenant.id, {
              admin_chat_id: adminChatId,
              ...(telegramBotToken.trim() ? { telegram_bot_token: telegramBotToken } : {}),
              ai_group_bot_enabled: aiGroupBotEnabled,
              notify_ai_failures_enabled: notifyAiFailuresEnabled,
            })}
          >
            保存 Bot 配置
          </Button>
          <Button disabled={!canManage || !configured || !adminChatId.trim()} loading={isActionPending(`tenant:${tenant.id}:bot:test`)} onClick={() => onTest(tenant.id)}>测试发送</Button>
          <Button disabled={!canManage || !configured} loading={isActionPending(`tenant:${tenant.id}:bot:webhook:refresh`)} onClick={() => onRefreshWebhook(tenant.id)}>刷新 webhook</Button>
          <Button danger disabled={!canManage || !configured} loading={isActionPending(`tenant:${tenant.id}:bot:webhook:delete`)} onClick={() => onDeleteWebhook(tenant.id)}>删除 webhook</Button>
        </Space>
        <Typography.Text type="secondary">测试发送只验证出站 sendMessage，不验证 Telegram 入站 webhook。</Typography.Text>
      </Space>
    </Card>
  );
}

function webhookStatusColor(status: string): string {
  if (status === 'registered') return 'green';
  if (['registration_failed', 'url_mismatch', 'query_failed'].includes(status)) return 'red';
  if (status === 'registering') return 'blue';
  return 'default';
}

export default function TelegramBotSettingsView({ tenants, botSettings, canManageBotSettings, onSaveTenantBotSettings, onTestTenantBotMessage, onRefreshTenantBotWebhook, onDeleteTenantBotWebhook, isActionPending }: Props) {
  return (
    <Card className="panel" title="TG Bot 配置" extra={<Typography.Text type="secondary">运营空间全局生效</Typography.Text>}>
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Typography.Paragraph type="secondary">先完成租户级 Bot 配置，再在 AI 活群任务详情或 TG bot 内设置话题、老师和连发策略。</Typography.Paragraph>
        <div className="cards-grid developer-grid">
          {tenants.map((tenant) => (
            <TenantBotCard
              key={tenant.id}
              tenant={tenant}
              settings={botSettings[tenant.id]}
              canManage={canManageBotSettings}
              isActionPending={isActionPending}
              onSave={onSaveTenantBotSettings}
              onTest={onTestTenantBotMessage}
              onRefreshWebhook={onRefreshTenantBotWebhook}
              onDeleteWebhook={onDeleteTenantBotWebhook}
            />
          ))}
        </div>
      </Space>
    </Card>
  );
}
