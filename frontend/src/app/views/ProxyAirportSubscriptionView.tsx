import React from 'react';
import { Alert, Button, Card, Descriptions, Input, Space } from 'antd';
import { api } from '../../shared/api/client';
import type { ProxyAirportSubscription } from '../types';

interface Props {
  canManageSystem?: boolean;
}

function errorText(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}

function proxyAirportReadinessLabel(config: ProxyAirportSubscription | null) {
  if (!config?.subscription_url_configured) return '未配置';
  if (config.last_error) return '节点同步失败';
  if (config.healthy_node_count > 0) return '健康节点可用';
  if (config.node_count > 0) return '同步成功但健康节点为 0';
  if (config.sync_status === 'test_pending') return '节点同步中';
  return '配置已保存，等待节点同步';
}

export default function ProxyAirportSubscriptionView({ canManageSystem = false }: Props) {
  const [config, setConfig] = React.useState<ProxyAirportSubscription | null>(null);
  const [subscriptionUrl, setSubscriptionUrl] = React.useState('');
  const [loading, setLoading] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState('');
  const [notice, setNotice] = React.useState('');

  React.useEffect(() => {
    void loadConfig();
  }, []);

  async function loadConfig() {
    setLoading(true);
    setError('');
    try {
      setConfig(await api<ProxyAirportSubscription>('/proxy-airport-subscription'));
    } catch (loadError) {
      setError(errorText(loadError));
    } finally {
      setLoading(false);
    }
  }

  async function saveConfig() {
    setSaving(true);
    setError('');
    try {
      const saved = await api<ProxyAirportSubscription>('/proxy-airport-subscription', {
        method: 'PATCH',
        body: JSON.stringify({ subscription_url: subscriptionUrl }),
      });
      setConfig(saved);
      setSubscriptionUrl('');
      setNotice('Clash 订阅配置已保存');
    } catch (saveError) {
      setError(errorText(saveError));
    } finally {
      setSaving(false);
    }
  }

  async function testConfig() {
    setSaving(true);
    setError('');
    try {
      const tested = await api<ProxyAirportSubscription>('/proxy-airport-subscription/test', { method: 'POST' });
      setConfig(tested);
      setNotice('订阅节点已解析，健康探测完成前不可作为可用代理池');
    } catch (testError) {
      setError(errorText(testError));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card
      className="panel"
      title="全局 Clash 订阅"
      extra={<Button size="small" onClick={loadConfig} loading={loading}>刷新</Button>}
    >
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
      {notice && <Alert type="success" showIcon message={notice} style={{ marginBottom: 12 }} />}
      <Descriptions
        bordered
        size="small"
        column={2}
        items={[
          { key: 'configured', label: '订阅状态', children: config?.subscription_url_configured ? '已配置' : '未配置' },
          { key: 'preview', label: '订阅地址', children: config?.subscription_url_preview || '-' },
          { key: 'readiness', label: '代理池状态', children: proxyAirportReadinessLabel(config) },
          { key: 'sync', label: '同步状态', children: config?.sync_status || '-' },
          { key: 'nodes', label: '节点', children: `${config?.healthy_node_count ?? 0}/${config?.node_count ?? 0}` },
          { key: 'updated', label: '更新时间', children: config?.updated_at ? config.updated_at.replace('T', ' ').slice(0, 16) : '-' },
          { key: 'error', label: '最近错误', children: config?.last_error || '-' },
        ]}
      />
      <Space direction="vertical" style={{ width: '100%', marginTop: 16 }}>
        <Input.Password
          value={subscriptionUrl}
          onChange={(event) => setSubscriptionUrl(event.target.value)}
          disabled={!canManageSystem}
          placeholder="https://example.com/clash/subscription"
        />
        <Space>
          <Button
            type="primary"
            onClick={saveConfig}
            loading={saving}
            disabled={!canManageSystem || !subscriptionUrl.trim()}
          >
            保存
          </Button>
          <Button onClick={testConfig} loading={saving} disabled={!canManageSystem || !config?.subscription_url_configured}>
            测试/同步
          </Button>
        </Space>
      </Space>
    </Card>
  );
}
