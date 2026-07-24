import React from 'react';
import { Alert, Button, Input, Modal, Space, Typography, message } from 'antd';
import type {
  OperationTarget,
  OperationTargetLifecycleImpact,
  OperationTargetLifecycleResult,
  OperationTargetLifecycleStatus,
} from '../types';
import { api } from '../../shared/api/client';

type Props = Readonly<{
  target: OperationTarget;
  canManage: boolean;
  onChanged: () => void | Promise<void>;
}>;

function lifecycleLabel(status: OperationTargetLifecycleStatus | undefined): string {
  if (status === 'group_dissolved') return '群已解散';
  if (status === 'target_ref_invalid') return '目标引用无效';
  return '正常';
}

function lifecycleColor(status: OperationTargetLifecycleStatus | undefined): string {
  if (status === 'group_dissolved') return 'error';
  if (status === 'target_ref_invalid') return 'warning';
  return 'success';
}

export function OperationTargetLifecycleTag({ target }: { target: OperationTarget }) {
  const status = target.lifecycle_status || 'active';
  const colorMap = {
    success: 'green',
    warning: 'orange',
    error: 'red',
  } as const;
  return (
    <span>
      <Typography.Text style={{ color: colorMap[lifecycleColor(status) as keyof typeof colorMap] }}>
        {lifecycleLabel(status)}
      </Typography.Text>
      {target.reference_revision ? (
        <Typography.Text type="secondary">{` · r${target.reference_revision}`}</Typography.Text>
      ) : null}
    </span>
  );
}

export default function OperationTargetLifecycleActions({ target, canManage, onChanged }: Props) {
  const [reason, setReason] = React.useState('');
  const [evidence, setEvidence] = React.useState('');
  const [peerId, setPeerId] = React.useState(target.tg_peer_id || '');
  const [username, setUsername] = React.useState(target.username || '');
  const [loading, setLoading] = React.useState(false);
  const status = target.lifecycle_status || 'active';
  const version = target.lifecycle_version || 1;

  React.useEffect(() => {
    setPeerId(target.tg_peer_id || '');
    setUsername(target.username || '');
  }, [target.id, target.reference_revision, target.tg_peer_id, target.username]);

  async function previewImpact(): Promise<OperationTargetLifecycleImpact | null> {
    try {
      return await api<OperationTargetLifecycleImpact>(`/operation-targets/${target.id}/lifecycle-impact-preview`, {
        method: 'POST',
      });
    } catch (error) {
      message.error(error instanceof Error ? error.message : '影响预览失败');
      return null;
    }
  }

  async function applyLifecycle(lifecycle_status: 'group_dissolved' | 'target_ref_invalid') {
    if (!reason.trim() || !evidence.trim()) {
      message.warning('请填写原因与证据引用');
      return;
    }
    const impact = await previewImpact();
    if (!impact) return;
    const isDissolve = lifecycle_status === 'group_dissolved';
    Modal.confirm({
      title: isDissolve ? '确认标记群已解散？' : '确认标记目标引用无效？',
      content: (
        <Space direction="vertical" size={4}>
          <Typography.Text>
            将影响：未开始动作 {impact.unstarted_action_count}、unknown {impact.unknown_action_count}、
            覆盖行 {impact.coverage_count}、单目标任务 {impact.single_target_task_count}
          </Typography.Text>
          <Typography.Text type="secondary">
            {isDissolve
              ? '文案将显示“群里已被解散，已跳过本目标”。仅在有独立外部证据时使用。'
              : '文案将引导引用修复，不会显示解散文案。'}
          </Typography.Text>
        </Space>
      ),
      okText: '确认提交',
      cancelText: '取消',
      onOk: async () => {
        setLoading(true);
        try {
          const result = await api<OperationTargetLifecycleResult>(`/operation-targets/${target.id}/lifecycle`, {
            method: 'PATCH',
            body: JSON.stringify({
              lifecycle_status,
              reason: reason.trim(),
              evidence_ref: evidence.trim(),
              expected_lifecycle_version: version,
            }),
          });
          message.success(
            isDissolve
              ? `群里已被解散，已跳过本目标（跳过 ${result.skipped_actions}）`
              : `目标引用无效已写入（跳过 ${result.skipped_actions}）`,
          );
          setReason('');
          setEvidence('');
          await onChanged();
        } catch (error) {
          message.error(error instanceof Error ? error.message : '生命周期更新失败');
        } finally {
          setLoading(false);
        }
      },
    });
  }

  async function reactivate() {
    if (!reason.trim() || !evidence.trim()) {
      message.warning('请填写原因与证据引用');
      return;
    }
    if (!peerId.trim() && !username.trim()) {
      message.warning('请提交重新核验后的 Peer 或 Username');
      return;
    }
    setLoading(true);
    try {
      await api<OperationTarget>(`/operation-targets/${target.id}/reactivate`, {
        method: 'POST',
        body: JSON.stringify({
          reason: reason.trim(),
          evidence_ref: evidence.trim(),
          expected_lifecycle_version: version,
          tg_peer_id: peerId.trim() || undefined,
          username: username.trim().replace(/^@/, '') || undefined,
        }),
      });
      message.success('已提交重新激活，请确认 can_send 后再规划');
      setReason('');
      setEvidence('');
      await onChanged();
    } catch (error) {
      message.error(error instanceof Error ? error.message : '重新激活失败');
    } finally {
      setLoading(false);
    }
  }

  if (!canManage) {
    return (
      <Alert
        type={status === 'active' ? 'info' : status === 'group_dissolved' ? 'error' : 'warning'}
        showIcon
        message={`生命周期：${lifecycleLabel(status)}`}
        description={target.lifecycle_reason || undefined}
      />
    );
  }

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Alert
        type={status === 'active' ? 'success' : status === 'group_dissolved' ? 'error' : 'warning'}
        showIcon
        message={`生命周期：${lifecycleLabel(status)} · version ${version}`}
        description={target.lifecycle_reason || '正常目标可继续经门禁发送'}
      />
      <Input.TextArea
        rows={2}
        placeholder="操作原因（必填）"
        value={reason}
        onChange={(event) => setReason(event.target.value)}
      />
      <Input
        placeholder="证据引用（必填，如 action id / 工单号）"
        value={evidence}
        onChange={(event) => setEvidence(event.target.value)}
      />
      {status !== 'active' && (
        <Space.Compact style={{ width: '100%' }}>
          <Input
            placeholder="重新核验后的 Peer / 邀请链接"
            value={peerId}
            onChange={(event) => setPeerId(event.target.value)}
          />
          <Input
            placeholder="Username（可替代 Peer）"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
          />
        </Space.Compact>
      )}
      <Space wrap>
        {target.target_type === 'group' && status !== 'group_dissolved' && (
          <Button danger loading={loading} onClick={() => applyLifecycle('group_dissolved')}>
            标记群已解散
          </Button>
        )}
        {status !== 'target_ref_invalid' && (
          <Button loading={loading} onClick={() => applyLifecycle('target_ref_invalid')}>
            标记引用无效
          </Button>
        )}
        {status !== 'active' && (
          <Button type="primary" loading={loading} onClick={reactivate}>
            重新激活
          </Button>
        )}
      </Space>
      <Typography.Text type="secondary">
        终态恢复必须提交重新核验后的引用并通过 can_send 检查；版本冲突会刷新后重新确认。禁止把 PEER_INVALID 自动当成解散。
      </Typography.Text>
    </Space>
  );
}
