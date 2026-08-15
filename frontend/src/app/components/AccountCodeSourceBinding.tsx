import React from 'react';
import { Button, Input, Modal, Space, Tag, Typography } from 'antd';
import { api } from '../../shared/api/client';
import type { Account } from '../types';

interface Props {
  account: Account;
  canReveal: boolean;
}

export function AccountCodeSourceBinding({ account, canReveal }: Props) {
  const [open, setOpen] = React.useState(false);
  const [reason, setReason] = React.useState('');
  const [uuid, setUuid] = React.useState('');
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState('');

  React.useEffect(() => {
    setOpen(false);
    setReason('');
    setUuid('');
    setError('');
  }, [account.id, account.code_source_binding_version]);

  function close() {
    setOpen(false);
    setReason('');
    setUuid('');
    setError('');
  }

  async function reveal() {
    if (!reason.trim()) return;
    setLoading(true);
    setError('');
    try {
      const result = await api<{ uuid: string }>(`/tg-accounts/${account.id}/code-source-binding/reveal`, {
        method: 'POST',
        body: JSON.stringify({ reason: reason.trim(), expected_binding_version: account.code_source_binding_version ?? 0 }),
      });
      setUuid(result.uuid);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '查看完整 UUID 失败');
    } finally {
      setLoading(false);
    }
  }

  if (!account.code_source_note) return <Typography.Text type="secondary">未绑定</Typography.Text>;
  return (
    <>
      <Space wrap>
        <Typography.Text>{account.code_source_note}</Typography.Text>
        <Tag>{account.code_source_binding_status}</Tag>
        {canReveal && <Button size="small" onClick={() => setOpen(true)}>查看完整 UUID</Button>}
      </Space>
      <Modal title="查看完整接码 UUID" open={open} onCancel={close} footer={uuid ? <Button onClick={close}>关闭并清除</Button> : undefined} okText="查看" confirmLoading={loading} onOk={() => void reveal()} okButtonProps={{ disabled: !reason.trim() }}>
        <Typography.Paragraph type="secondary">查看会记录操作原因；完整值只在当前弹窗临时显示。</Typography.Paragraph>
        {!uuid && <Input value={reason} onChange={(event) => setReason(event.target.value)} placeholder="查看原因（必填）" />}
        {uuid && <Typography.Text copyable>{uuid}</Typography.Text>}
        {error && <Typography.Paragraph type="danger">{error}</Typography.Paragraph>}
      </Modal>
    </>
  );
}
