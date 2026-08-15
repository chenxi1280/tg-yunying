import React from 'react';
import { Alert, Button, Space, Typography } from 'antd';
import { api } from '../../shared/api/client';
import type { AccountBatchLoginNotification, AccountBatchLoginNoticeItem } from '../types';

interface Props {
  enabled: boolean;
}

export function AccountBatchLoginNotifications({ enabled }: Props) {
  const [rows, setRows] = React.useState<AccountBatchLoginNotification[]>([]);
  const [error, setError] = React.useState('');

  React.useEffect(() => {
    if (!enabled) {
      setRows([]);
      return;
    }
    let disposed = false;
    const load = async () => {
      try {
        const result = await api<AccountBatchLoginNotification[]>('/tg-accounts/login-batch-notifications?unacknowledged=true');
        if (!disposed) {
          setRows(result);
          setError('');
        }
      } catch (requestError) {
        if (!disposed) setError(requestError instanceof Error ? requestError.message : '读取批量登录提醒失败');
      }
    };
    void load();
    const timer = window.setInterval(load, 10_000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [enabled]);

  async function acknowledge(row: AccountBatchLoginNotification) {
    try {
      await api(`/tg-accounts/login-batch-notifications/${row.id}/ack`, {
        method: 'POST',
        body: JSON.stringify({ expected_version: row.state_version }),
      });
      setRows((current) => current.filter((item) => item.id !== row.id));
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '确认提醒失败');
    }
  }

  if (!enabled || (!rows.length && !error)) return null;
  if (error && !rows.length) return <Alert className="runtime-strip" type="error" showIcon message="批量登录提醒读取失败" description={error} />;
  const row = rows[0];
  const summary = row.summary;
  const detail = [
    correctionSection(summary.corrections || []),
    section('失败', summary.failed),
    section('未解', summary.unresolved),
    section('警告', summary.warning),
  ].filter(Boolean).join('；');
  return (
    <Alert
      className="runtime-strip"
      type={summary.unresolved.length || summary.failed.length ? 'warning' : 'success'}
      showIcon
      title={row.event_type === 'correction' ? `批量登录 #${row.batch_id} 结果已更正` : `批量登录 #${row.batch_id} 已完成`}
      description={(
        <Space orientation="vertical" size={4}>
          <Typography.Text>{`成功 ${summary.counts.success || 0} / 失败 ${summary.counts.failed || 0} / 未解 ${summary.counts.unresolved || 0} / 警告 ${summary.counts.warning || 0} / 跳过 ${summary.counts.skipped || 0}`}</Typography.Text>
          <Typography.Text>{detail || '全部账号登录成功。'}</Typography.Text>
          {summary.tg_bot_delivery === 'dead_letter' && <Typography.Text type="danger">TG Bot 提醒发送失败，平台提醒仍已保留。</Typography.Text>}
          {rows.length > 1 && <Typography.Text type="secondary">另有 {rows.length - 1} 条未确认提醒</Typography.Text>}
        </Space>
      )}
      action={<Button size="small" onClick={() => void acknowledge(row)}>确认已读</Button>}
    />
  );
}

function section(label: string, items: AccountBatchLoginNoticeItem[]) {
  if (!items.length) return '';
  const values = items.map((item) => `第${item.line_no}行 ${item.phone_masked}（${item.reason}）`);
  return `${label}：${values.join('、')}`;
}

function correctionSection(items: NonNullable<AccountBatchLoginNotification['summary']['corrections']>) {
  if (!items.length) return '';
  const values = items.map((item) => `第${item.line_no}行 ${item.phone_masked}（${item.from_status}→${item.to_status}：${item.reason}）`);
  return `更正：${values.join('、')}`;
}
