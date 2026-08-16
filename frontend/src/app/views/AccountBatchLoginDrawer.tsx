import React from 'react';
import { Alert, Button, Checkbox, Descriptions, Drawer, Input, Modal, Space, Table, Tag, Typography, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { api } from '../../shared/api/client';
import type { Account, AccountBatchLogin, AccountBatchLoginItem, AccountDetail, AccountPool } from '../types';
import { formatBeijingDateTime } from '../time';
import { loginStatusColor, loginStatusLabel, TERMINAL_LOGIN_BATCH_STATUSES } from './accountBatchLoginPresentation';

const LOGIN_BATCH_DETAIL_ITEM_LIMIT = 200;

interface Props {
  batchId: number | null;
  pools: AccountPool[];
  onClose: () => void;
  onOpenAccountDetail: (account: Account) => void;
}

export function AccountBatchLoginDrawer({ batchId, pools, onClose, onOpenAccountDetail }: Props) {
  const [batch, setBatch] = React.useState<AccountBatchLogin | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState('');
  const [refreshItem, setRefreshItem] = React.useState<AccountBatchLoginItem | null>(null);
  const [credentialUrl, setCredentialUrl] = React.useState('');
  const [reason, setReason] = React.useState('');
  const [replaceBinding, setReplaceBinding] = React.useState(false);

  React.useEffect(() => {
    if (!batchId) return;
    let disposed = false;
    let timer: number | undefined;
    const load = async () => {
      try {
        const detail = await api<AccountBatchLogin>(detailPath(batchId));
        if (disposed) return;
        setBatch(detail);
        setError('');
        if (!TERMINAL_LOGIN_BATCH_STATUSES.has(detail.status)) timer = window.setTimeout(load, 5000);
      } catch (requestError) {
        if (!disposed) setError(requestError instanceof Error ? requestError.message : '读取批次失败');
      }
    };
    void load();
    return () => {
      disposed = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [batchId]);

  async function reload() {
    if (!batchId) return;
    setLoading(true);
    try {
      setBatch(await api<AccountBatchLogin>(detailPath(batchId)));
      setError('');
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '读取批次失败');
    } finally {
      setLoading(false);
    }
  }

  async function retryItem(item: AccountBatchLoginItem) {
    if (!batch) return;
    setLoading(true);
    try {
      await api(`/tg-accounts/login-batches/${batch.id}/retry`, {
        method: 'POST',
        body: JSON.stringify({
          item_ids: [item.id],
          expected_state_version: batch.state_version,
          expected_attempt_id: item.status === 'unresolved' ? item.current_attempt_id : null,
          expected_attempt_version: item.status === 'unresolved' ? item.current_attempt_state_version : null,
          expected_resolution_version: item.status === 'unresolved' ? batch.resolution_version : null,
          confirm_remote_unknown: item.status === 'unresolved',
          reason: '操作员从批次详情重试',
        }),
      });
      void message.success(`第 ${item.line_no} 行已重新排队`);
      await reload();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '重试失败');
    } finally {
      setLoading(false);
    }
  }

  async function cancelBatch() {
    if (!batch) return;
    setLoading(true);
    try {
      await api(`/tg-accounts/login-batches/${batch.id}/cancel`, {
        method: 'POST',
        body: JSON.stringify({ expected_state_version: batch.state_version, reason: '操作员取消批次' }),
      });
      await reload();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '取消批次失败');
    } finally {
      setLoading(false);
    }
  }

  async function refreshCredential() {
    if (!batch || !refreshItem || !credentialUrl.trim() || !reason.trim()) return;
    setLoading(true);
    try {
      await api(`/tg-accounts/login-batches/${batch.id}/items/${refreshItem.id}/refresh-credential`, {
        method: 'POST',
        body: JSON.stringify({
          code_url: credentialUrl.trim(),
          expected_item_version: refreshItem.state_version,
          expected_binding_version: replaceBinding ? refreshItem.account_binding_version : null,
          replace_binding: replaceBinding,
          reason: reason.trim(),
        }),
      });
      setRefreshItem(null);
      setCredentialUrl('');
      setReason('');
      setReplaceBinding(false);
      void message.success('接码地址已刷新；系统不会自动重试该行');
      await reload();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '刷新接码地址失败');
    } finally {
      setLoading(false);
    }
  }

  async function openAccount(item: AccountBatchLoginItem) {
    if (!item.account_id) return;
    try {
      const detail = await api<AccountDetail>(`/tg-accounts/${item.account_id}/detail`);
      onClose();
      onOpenAccountDetail(detail.account);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '读取账号失败');
    }
  }

  const columns: ColumnsType<AccountBatchLoginItem> = [
    { title: '行', dataIndex: 'line_no', width: 60 },
    { title: '手机号', dataIndex: 'phone_masked', width: 145 },
    { title: '接码备注', dataIndex: 'code_source_note', width: 210 },
    { title: '路由', width: 145, render: (_, item) => routeLabel(item.route || item.route_hint) },
    { title: '阶段', dataIndex: 'phase', width: 145 },
    { title: '代次/重试', width: 100, render: (_, item) => `${item.execution_generation} / ${item.retry_count}` },
    { title: '下次执行', width: 170, render: (_, item) => item.next_retry_at ? formatBeijingDateTime(item.next_retry_at) : '—' },
    { title: '状态', width: 145, render: (_, item) => <Tag color={loginStatusColor(item.status)}>{loginStatusLabel(item.status)}</Tag> },
    { title: '结果', width: 260, render: (_, item) => item.failure_detail || item.warning_detail || '—' },
    {
      title: '操作',
      width: 180,
      render: (_, item) => (
        <Space wrap>
          {canRetry(item) && <Button size="small" onClick={() => void retryItem(item)}>重试</Button>}
          {['failed', 'unresolved'].includes(item.status) && <Button size="small" onClick={() => setRefreshItem(item)}>换接码地址</Button>}
          {item.account_id && <Button size="small" onClick={() => void openAccount(item)}>查看账号</Button>}
        </Space>
      ),
    },
  ];

  const targetPool = pools.find((pool) => pool.id === batch?.pool_id);
  return (
    <>
      <Drawer title={batch ? `批量登录 #${batch.id}` : '批量登录'} open={batchId !== null} width={1120} onClose={onClose}>
        {error && <Alert type="error" showIcon message={error} />}
        {batch && (
          <>
            <Descriptions size="small" column={4} bordered items={[
              { key: 'status', label: '状态', children: loginStatusLabel(batch.status) },
              { key: 'pool', label: '目标分组', children: targetPool?.name || `#${batch.pool_id}` },
              { key: 'counts', label: '结果', children: `成功 ${batch.success_count} / 失败 ${batch.failed_count} / 未解 ${batch.unresolved_count} / 警告 ${batch.warning_count} / 跳过 ${batch.skipped_count}` },
              { key: 'created', label: '创建时间', children: formatBeijingDateTime(batch.created_at) },
            ]} />
            {batch.unresolved_count > 0 && <Alert showIcon type="warning" title="存在远程结果未解行" description="这些行已经让出批内顺序，后台会持续对账；权威结果变化时会发送更正提醒。" />}
            <Space style={{ margin: '16px 0' }}>
              <Button loading={loading} onClick={() => void reload()}>刷新</Button>
              {!TERMINAL_LOGIN_BATCH_STATUSES.has(batch.status) && <Button danger loading={loading} onClick={() => void cancelBatch()}>取消批次</Button>}
            </Space>
            <Table rowKey="id" columns={columns} dataSource={batch.items || []} pagination={false} scroll={{ x: 1600 }} />
          </>
        )}
      </Drawer>
      <Modal title={`刷新第 ${refreshItem?.line_no ?? ''} 行接码地址`} open={refreshItem !== null} confirmLoading={loading} okText="只更新凭据" onOk={() => void refreshCredential()} onCancel={() => { setRefreshItem(null); setReplaceBinding(false); }}>
        <Typography.Paragraph type="secondary">更新后不会自动发送验证码或重试，请回到批次行显式点击重试。</Typography.Paragraph>
        <Input.TextArea value={credentialUrl} onChange={(event) => setCredentialUrl(event.target.value)} placeholder="https://tgbotchecker.com/GetHTML?uuid=..." rows={3} />
        <Checkbox checked={replaceBinding} onChange={(event) => setReplaceBinding(event.target.checked)} style={{ marginTop: 12 }}>新地址使用不同 UUID，确认替换该账号的接码绑定</Checkbox>
        <Input value={reason} onChange={(event) => setReason(event.target.value)} placeholder="操作原因（必填）" style={{ marginTop: 12 }} />
      </Modal>
    </>
  );
}

function canRetry(item: AccountBatchLoginItem) {
  if (item.retry_count >= 3) return false;
  if (item.status === 'failed') return true;
  return item.status === 'unresolved' && item.reconcile_attempted && ['pending', 'exhausted', 'manual_review_required'].includes(item.reconcile_status);
}

function routeLabel(route: string) {
  const labels: Record<string, string> = {
    create: '新建账号',
    existing_probe_required: '已有账号待探测',
    relogin: '已有账号重登',
    already_authorized: '已有账号已授权',
  };
  return labels[route] || route || '—';
}

function detailPath(batchId: number) {
  return `/tg-accounts/login-batches/${batchId}?item_limit=${LOGIN_BATCH_DETAIL_ITEM_LIMIT}&item_offset=0`;
}
