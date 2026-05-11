import React from 'react';
import { Alert, Button, Card, Descriptions, Form, Input, Modal, Space, Table, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { CheckCircle2, Database, RefreshCcw, ShieldAlert } from 'lucide-react';
import { api } from '../../shared/api/client';
import { StatCard, StatusBadge, useAntdTableControls } from '../components/shared';
import type { RuleSet } from '../types';

type RuleRow = {
  key: string;
  category: string;
  name: string;
  status: string;
  detail: string;
  version: string;
  source: string;
  metadata: Record<string, any>;
};

type RuleSummary = {
  system_rule_count: number;
  keyword_rule_count: number;
  relay_task_rule_count: number;
  items: RuleRow[];
};

type RuleTestResult = {
  result: string;
  hits: Array<{ rule_id: number; keyword: string; match_type: string; note: string }>;
};

export default function RulesCenterView({ onOpenSystemConfig }: { onOpenSystemConfig: () => void }) {
  const [summary, setSummary] = React.useState<RuleSummary>({ system_rule_count: 0, keyword_rule_count: 0, relay_task_rule_count: 0, items: [] });
  const [ruleSets, setRuleSets] = React.useState<RuleSet[]>([]);
  const [sample, setSample] = React.useState('');
  const [testResult, setTestResult] = React.useState<RuleTestResult>({ result: '未测试', hits: [] });
  const [loading, setLoading] = React.useState(false);
  const [testing, setTesting] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [createOpen, setCreateOpen] = React.useState(false);
  const [versionTarget, setVersionTarget] = React.useState<RuleSet | null>(null);
  const [error, setError] = React.useState('');
  const [createForm] = Form.useForm();
  const [versionForm] = Form.useForm();

  async function load() {
    setLoading(true);
    setError('');
    try {
      const [nextSummary, nextRuleSets] = await Promise.all([
        api<RuleSummary>('/rules/summary'),
        api<RuleSet[]>('/rule-sets'),
      ]);
      setSummary(nextSummary);
      setRuleSets(nextRuleSets);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  React.useEffect(() => {
    void load();
  }, []);

  async function runRuleTest() {
    setTesting(true);
    setError('');
    try {
      setTestResult(await api<RuleTestResult>('/rules/test', { method: 'POST', body: JSON.stringify({ text: sample }) }));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setTesting(false);
    }
  }

  function ruleConfig(values: Record<string, string>) {
    const read = (key: string) => {
      const raw = values[key]?.trim() || '{}';
      const parsed = JSON.parse(raw);
      if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') {
        throw new Error(`${key} 必须是 JSON 对象`);
      }
      return parsed;
    };
    return {
      filters: read('filters'),
      transforms: read('transforms'),
      routing: read('routing'),
      account_strategy: read('account_strategy'),
      rate_limits: read('rate_limits'),
      retry_policy: read('retry_policy'),
    };
  }

  async function createRuleSet() {
    setSaving(true);
    setError('');
    try {
      const values = await createForm.validateFields();
      await api<RuleSet>('/rule-sets', { method: 'POST', body: JSON.stringify({ name: values.name, description: values.description ?? '', ...ruleConfig(values) }) });
      setCreateOpen(false);
      createForm.resetFields();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  async function createRuleSetVersion() {
    if (!versionTarget) return;
    setSaving(true);
    setError('');
    try {
      const values = await versionForm.validateFields();
      await api<RuleSet>(`/rule-sets/${versionTarget.id}/versions`, { method: 'POST', body: JSON.stringify(ruleConfig(values)) });
      setVersionTarget(null);
      versionForm.resetFields();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  async function publishRuleSetVersion(ruleSet: RuleSet, versionId: number) {
    setSaving(true);
    setError('');
    try {
      await api<RuleSet>(`/rule-sets/${ruleSet.id}/versions/${versionId}/publish`, { method: 'POST' });
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  const table = useAntdTableControls<RuleRow>({
    rows: summary.items,
    placeholder: '搜索规则 / 版本 / 状态',
    search: ['category', 'name', 'status', 'detail', 'version'],
  });
  const columns: ColumnsType<RuleRow> = [
    { title: '规则类别', dataIndex: 'category', width: 150 },
    {
      title: '规则',
      key: 'rule',
      width: 260,
      render: (_, row) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{row.name}</Typography.Text>
          <Typography.Text type="secondary">版本 {row.version}</Typography.Text>
        </Space>
      ),
    },
    { title: '状态', dataIndex: 'status', width: 120, render: (value) => <StatusBadge status={value} /> },
    { title: '处理口径', dataIndex: 'detail' },
  ];
  const ruleSetTable = useAntdTableControls<RuleSet>({
    rows: ruleSets,
    placeholder: '搜索规则集 / 状态 / 描述',
    search: ['name', 'description', 'status'],
  });
  const ruleSetColumns: ColumnsType<RuleSet> = [
    {
      title: '规则集',
      key: 'name',
      render: (_, row) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{row.name}</Typography.Text>
          <Typography.Text type="secondary">{row.description || '过滤、转换、路由、账号策略、限速、重试'}</Typography.Text>
        </Space>
      ),
    },
    { title: '状态', dataIndex: 'status', width: 120, render: (value) => <StatusBadge status={value} /> },
    { title: '活动版本', dataIndex: 'active_version_id', width: 120, render: (_, row) => row.versions.find((version) => version.id === row.active_version_id)?.version ? `v${row.versions.find((version) => version.id === row.active_version_id)?.version}` : '-' },
    { title: '版本数', key: 'versions', width: 100, render: (_, row) => row.versions.length },
    { title: '更新时间', dataIndex: 'updated_at', width: 180, render: (value) => value ? new Date(value).toLocaleString() : '-' },
    { title: '操作', key: 'actions', width: 140, render: (_, row) => <Button size="small" icon={<CheckCircle2 size={14} />} onClick={() => { setVersionTarget(row); versionForm.setFieldsValue(defaultRuleJson()); }}>新建版本</Button> },
  ];

  return (
    <section className="view-grid">
      <Space className="stats-grid" wrap>
        <StatCard label="系统规则" value={summary.system_rule_count} detail="自动校验、路由、账号、重试" icon={<ShieldAlert size={20} />} />
        <StatCard label="规则集" value={ruleSets.length} detail="过滤、转换、路由版本化" icon={<CheckCircle2 size={20} />} />
        <StatCard label="关键词规则" value={summary.keyword_rule_count} detail="接入系统设置配置" icon={<Database size={20} />} />
        <StatCard label="转发任务规则" value={summary.relay_task_rule_count} detail="任务绑定的过滤/转换配置" icon={<CheckCircle2 size={20} />} />
      </Space>
      <Card className="panel" title="规则集版本管理" extra={<Space><Button type="primary" icon={<CheckCircle2 size={16} />} onClick={() => { createForm.setFieldsValue(defaultRuleJson()); setCreateOpen(true); }}>新建规则集</Button><Button icon={<RefreshCcw size={16} />} loading={loading} onClick={load}>刷新</Button></Space>}>
        {error && <Alert className="form-alert" type="error" showIcon message={error} />}
        <Space className="toolbar-row" wrap>{ruleSetTable.searchInput}</Space>
        <Table<RuleSet>
          className="tg-table"
          rowKey="id"
          columns={ruleSetColumns}
          dataSource={ruleSetTable.filteredRows}
          pagination={ruleSetTable.pagination}
          scroll={{ x: 980 }}
          loading={loading}
          expandable={{
            expandedRowRender: (row) => (
              <Table
                rowKey="id"
                size="small"
                pagination={false}
                dataSource={row.versions}
                columns={[
                  { title: '版本', key: 'version', width: 90, render: (_, version) => `v${version.version}` },
                  { title: '状态', dataIndex: 'status', width: 120, render: (value) => <StatusBadge status={value} /> },
                  { title: '过滤', dataIndex: 'filters', render: (value) => JSON.stringify(value) },
                  { title: '转换', dataIndex: 'transforms', render: (value) => JSON.stringify(value) },
                  { title: '路由', dataIndex: 'routing', render: (value) => JSON.stringify(value) },
                  { title: '操作', key: 'actions', width: 110, render: (_, version) => version.status === 'published' ? '当前发布' : <Button size="small" loading={saving} onClick={() => publishRuleSetVersion(row, version.id)}>发布</Button> },
                ]}
                scroll={{ x: 980 }}
              />
            ),
          }}
        />
      </Card>
      <Card className="panel" title="规则中心" extra={<Space><Button icon={<ShieldAlert size={16} />} onClick={onOpenSystemConfig}>管理关键词</Button><Button icon={<RefreshCcw size={16} />} loading={loading} onClick={load}>刷新</Button></Space>}>
        {error && <Alert className="form-alert" type="error" showIcon message={error} />}
        <Space className="toolbar-row" wrap>{table.searchInput}</Space>
        <Table<RuleRow>
          className="tg-table"
          rowKey="key"
          columns={columns}
          dataSource={table.filteredRows}
          pagination={table.pagination}
          scroll={{ x: 960 }}
          loading={loading}
        />
      </Card>
      <Card className="panel" title="规则测试器" extra={<Typography.Text type="secondary">当前支持关键词命中预览</Typography.Text>}>
        <Input.TextArea rows={4} value={sample} onChange={(event) => setSample(event.target.value)} placeholder="输入一条源群消息，预览系统关键词规则命中情况" />
        <Space className="modal-actions">
          <Button type="primary" loading={testing} onClick={runRuleTest}>测试规则</Button>
        </Space>
        <Descriptions
          className="rule-test-result"
          bordered
          size="small"
          column={2}
          items={[
            { key: 'result', label: '过滤结果', children: testResult.result },
            { key: 'hits', label: '命中规则', children: testResult.hits.map((rule) => rule.keyword).join('、') || '无' },
          ]}
        />
      </Card>
      <Modal className="tg-modal large" title="新建规则集" open={createOpen} width={840} confirmLoading={saving} okText="创建并发布 v1" cancelText="取消" onOk={createRuleSet} onCancel={() => setCreateOpen(false)} destroyOnHidden centered>
        <RuleSetForm form={createForm} includeBasics />
      </Modal>
      <Modal className="tg-modal large" title={versionTarget ? `新建版本：${versionTarget.name}` : '新建版本'} open={Boolean(versionTarget)} width={840} confirmLoading={saving} okText="保存未发布版本" cancelText="取消" onOk={createRuleSetVersion} onCancel={() => setVersionTarget(null)} destroyOnHidden centered>
        <RuleSetForm form={versionForm} />
      </Modal>
    </section>
  );
}

function defaultRuleJson() {
  return {
    filters: '{}',
    transforms: '{}',
    routing: '{}',
    account_strategy: '{"mode":"target_sticky"}',
    rate_limits: '{}',
    retry_policy: '{"max_retries":3}',
  };
}

function RuleSetForm({ form, includeBasics = false }: { form: ReturnType<typeof Form.useForm>[0]; includeBasics?: boolean }) {
  return (
    <Form form={form} layout="vertical" initialValues={defaultRuleJson()}>
      {includeBasics && (
        <div className="form-grid">
          <Form.Item name="name" label="规则集名称" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="description" label="说明"><Input /></Form.Item>
        </div>
      )}
      <div className="form-grid">
        <Form.Item name="filters" label="过滤规则 JSON"><Input.TextArea rows={4} /></Form.Item>
        <Form.Item name="transforms" label="转换规则 JSON"><Input.TextArea rows={4} /></Form.Item>
        <Form.Item name="routing" label="路由规则 JSON"><Input.TextArea rows={4} /></Form.Item>
        <Form.Item name="account_strategy" label="账号策略 JSON"><Input.TextArea rows={4} /></Form.Item>
        <Form.Item name="rate_limits" label="限速策略 JSON"><Input.TextArea rows={4} /></Form.Item>
        <Form.Item name="retry_policy" label="重试策略 JSON"><Input.TextArea rows={4} /></Form.Item>
      </div>
    </Form>
  );
}
