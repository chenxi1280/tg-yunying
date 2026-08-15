import React from 'react';
import { Alert, Button, Checkbox, Descriptions, Input, Modal, Select, Space, Table, Typography, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { api } from '../../shared/api/client';
import type { Account, AccountBatchLogin, AccountBatchLoginCapability, AccountBatchLoginPreview, AccountBatchLoginPreviewItem, AccountPool } from '../types';
import { AccountBatchLoginDrawer } from './AccountBatchLoginDrawer';

interface Props {
  pools: AccountPool[];
  selectedPoolId: number | '';
  disabled?: boolean;
  onOpenAccountDetail: (account: Account) => void;
}

export function AccountBatchLoginControl({ pools, selectedPoolId, disabled = false, onOpenAccountDetail }: Props) {
  const [open, setOpen] = React.useState(false);
  const [poolId, setPoolId] = React.useState<number | ''>('');
  const [linesText, setLinesText] = React.useState('');
  const [reason, setReason] = React.useState('');
  const [capability, setCapability] = React.useState<AccountBatchLoginCapability | null>(null);
  const [preview, setPreview] = React.useState<AccountBatchLoginPreview | null>(null);
  const [replaceLines, setReplaceLines] = React.useState<number[]>([]);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState('');
  const [batchId, setBatchId] = React.useState<number | null>(null);
  const [idempotencyKey, setIdempotencyKey] = React.useState('');
  const enabledPools = pools.filter((pool) => pool.is_enabled);
  const local = localLineStats(linesText);

  async function showModal() {
    setOpen(true);
    setPoolId(typeof selectedPoolId === 'number' ? selectedPoolId : enabledPools[0]?.id || '');
    setPreview(null);
    setError('');
    try {
      setCapability(await api<AccountBatchLoginCapability>('/tg-accounts/login-batches/capability'));
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '读取批量登录能力失败');
    }
  }

  async function precheck() {
    if (!poolId || !linesText.trim()) return;
    setLoading(true);
    setError('');
    try {
      const result = await api<AccountBatchLoginPreview>('/tg-accounts/login-batches/precheck', {
        method: 'POST',
        body: JSON.stringify({ pool_id: poolId, lines_text: linesText }),
        timeoutMs: 30_000,
      });
      setPreview(result);
      setIdempotencyKey(crypto.randomUUID());
      setReplaceLines([]);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '预检失败');
    } finally {
      setLoading(false);
    }
  }

  async function createBatch() {
    if (!preview || !poolId || !reason.trim() || !idempotencyKey) return;
    const required = preview.items.filter((item) => item.binding_action === 'replace_required');
    if (required.some((item) => !replaceLines.includes(item.line_no))) {
      setError('请确认所有接码绑定替换项');
      return;
    }
    setLoading(true);
    setError('');
    try {
      const batch = await api<AccountBatchLogin>('/tg-accounts/login-batches', {
        method: 'POST',
        body: JSON.stringify({
          pool_id: poolId,
          lines_text: linesText,
          binding_decisions: required.map((item) => ({ line_no: item.line_no, replace_binding: true, expected_binding_version: item.current_binding_version })),
          preview_token: preview.preview_token,
          preview_fingerprint: preview.preview_fingerprint,
          idempotency_key: idempotencyKey,
          reason: reason.trim(),
        }),
      });
      setOpen(false);
      setBatchId(batch.id);
      setLinesText('');
      setReason('');
      setPreview(null);
      setIdempotencyKey('');
      void message.success('批次已创建，正在按行执行');
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '创建批次失败');
    } finally {
      setLoading(false);
    }
  }

  const columns: ColumnsType<AccountBatchLoginPreviewItem> = [
    { title: '行', dataIndex: 'line_no', width: 60 },
    { title: '手机号', dataIndex: 'phone_masked', width: 150 },
    { title: '候选路由', dataIndex: 'route_hint', width: 190, render: (value) => value === 'create' ? '新建账号' : '已有账号，执行时权威探测' },
    { title: '接码备注', dataIndex: 'code_source_note', width: 210 },
    {
      title: '绑定变化',
      render: (_, item) => item.binding_action === 'replace_required' ? (
        <Checkbox checked={replaceLines.includes(item.line_no)} onChange={(event) => setReplaceLines((current) => event.target.checked ? [...current, item.line_no] : current.filter((value) => value !== item.line_no))}>
          替换 {item.current_binding_note}
        </Checkbox>
      ) : item.binding_action === 'keep' ? '保持当前绑定' : '新增绑定',
    },
  ];

  return (
    <>
      <Button type="primary" disabled={disabled} onClick={() => void showModal()}>批量登录</Button>
      <Modal title="批量登录账号" open={open} width={920} footer={null} destroyOnHidden onCancel={() => setOpen(false)}>
        {error && <Alert type="error" showIcon message={error} />}
        {capability && (!capability.readiness || capability.mode !== 'enabled') && <Alert type="warning" showIcon title="批量登录当前不可用" description={`模式：${capability.mode}；阻塞：${capability.blockers.join('、') || '无'}`} />}
        {!preview ? (
          <Space orientation="vertical" size={12} style={{ width: '100%' }}>
            <label>目标分组<Select style={{ width: '100%' }} value={poolId} onChange={setPoolId} options={enabledPools.map((pool) => ({ value: pool.id, label: pool.name }))} /></label>
            <label>账号与接码地址<Input.TextArea rows={10} value={linesText} onChange={(event) => setLinesText(event.target.value)} placeholder={'+12025550123|https://tgbotchecker.com/GetHTML?uuid=<32位uuid>'} /></label>
            <Typography.Text type="secondary">共 {local.total} 行 / 格式有效 {local.valid} 行 / 手机号重复 {local.phoneDuplicates} 行 / UUID 重复 {local.uuidDuplicates} 行</Typography.Text>
            <Space><Button onClick={() => setOpen(false)}>取消</Button><Button type="primary" loading={loading} disabled={!poolId || !linesText.trim() || capability?.readiness !== true || capability.mode !== 'enabled'} onClick={() => void precheck()}>预检并确认</Button></Space>
          </Space>
        ) : (
          <Space orientation="vertical" size={12} style={{ width: '100%' }}>
            <Descriptions bordered size="small" column={3} items={[
              { key: 'route', label: '路由候选', children: `新建 ${preview.create_count} / 已有待探测 ${preview.existing_probe_required_count}` },
              { key: 'queue', label: '排队位置', children: `第 ${preview.queue_position} 位` },
              { key: 'time', label: '最坏耗时', children: formatDuration(preview.worst_case_seconds) },
            ]} />
            <Alert type="info" showIcon title="预检不会访问 Telegram 或接码站" description="供应页不提供手机号，无法验证地址与号码是否对应，请确认粘贴映射正确。已有账号会由 worker 做新鲜权威探测；单行失败、120 秒验证码超时或 300 秒未解不会阻塞下一行。" />
            <Table rowKey="line_no" columns={columns} dataSource={preview.items} pagination={false} scroll={{ x: 850, y: 320 }} />
            <Input.TextArea rows={2} maxLength={255} showCount value={reason} onChange={(event) => setReason(event.target.value)} placeholder="操作原因（必填）" />
            <Space><Button onClick={() => setPreview(null)}>返回修改</Button><Button type="primary" loading={loading} disabled={!reason.trim()} onClick={() => void createBatch()}>确认创建批次</Button></Space>
          </Space>
        )}
      </Modal>
      <AccountBatchLoginDrawer batchId={batchId} pools={pools} onOpenAccountDetail={onOpenAccountDetail} onClose={() => setBatchId(null)} />
    </>
  );
}

function localLineStats(linesText: string) {
  const rows = linesText.split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
  const parsed = rows.map(parseLocalLine).filter((value) => value !== null);
  const phones = parsed.map((value) => value.phone);
  const uuids = parsed.map((value) => value.uuid);
  return { total: rows.length, valid: parsed.length, phoneDuplicates: phones.length - new Set(phones).size, uuidDuplicates: uuids.length - new Set(uuids).size };
}

function parseLocalLine(row: string) {
  if ((row.match(/\|/g) || []).length !== 1) return null;
  const [phoneValue, urlValue] = row.split('|');
  const phone = phoneValue.replace(/[\s()-]/g, '');
  let url = urlValue.trim();
  if (url.startsWith('`') && url.endsWith('`')) url = url.slice(1, -1).trim();
  const urlMatch = url.match(/^https:\/\/tgbotchecker\.com\/GetHTML\?uuid=([0-9a-fA-F]{32})$/);
  if (!/^\+[1-9]\d{7,14}$/.test(phone) || !urlMatch) return null;
  return { phone, uuid: urlMatch[1].toLowerCase() };
}

function formatDuration(seconds: number) {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.ceil((seconds % 3600) / 60);
  return hours ? `约 ${hours} 小时 ${minutes} 分钟` : `约 ${minutes} 分钟`;
}
