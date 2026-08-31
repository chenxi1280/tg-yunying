import React from 'react';
import { Alert, Button, Descriptions, Form, Input, InputNumber, Select, Space } from 'antd';
import type { FormInstance } from 'antd';
import { api } from '../../shared/api/client';
import type { TaskCenterTask } from '../types';
import { errorMessage } from './taskCenterViewModel';

type RouteManifest = {
  source_peer_type: string; source_peer_id: string;
  target_peer_type: string; target_peer_id: string;
};

type CutoverPreview = {
  preview_token: string; legacy_task_id: string; expected_legacy_revision: number;
  route_manifest_hash: string; expected_authority_version: number;
  open_action_fingerprint: string; route_manifest: RouteManifest;
};

type CutoverFields = {
  source_internal_group_id: number; source_operation_target_id: number;
  listener_account_id: number; listener_authorization_id: number;
  authorization_mode: 'public' | 'owned' | 'admin_authorized';
  target_internal_group_id: number; target_operation_target_id: number;
  control_account_id: number; control_authorization_id: number;
  sender_pool_account_ids: string; rule_set_id: number; rule_set_version: number;
  reason: string;
};

function requestId(prefix: string): string {
  return `${prefix}-${crypto.randomUUID()}`;
}

function accountIds(value: string): number[] | null {
  const tokens = value.split(/[,，\n\s]+/).filter(Boolean);
  const values = tokens.map(Number);
  if (!values.length || values.some((item) => !Number.isInteger(item) || item <= 0)) return null;
  return [...new Set(values)];
}

type CutoverProps = {
  task: TaskCenterTask; canManageTasks: boolean; onChanged: () => void;
};

function useCutoverPanel(props: CutoverProps) {
  const [form] = Form.useForm<CutoverFields>();
  const [preview, setPreview] = React.useState<CutoverPreview | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [notice, setNotice] = React.useState<{ type: 'success' | 'error'; text: string } | null>(null);

  async function loadPreview() {
    setBusy(true);
    setNotice(null);
    try {
      const result = await api<CutoverPreview>(`/tasks/${props.task.id}/group-clone/cutover/preview`, { method: 'POST' });
      setPreview(result);
    } catch (error) {
      setNotice({ type: 'error', text: errorMessage(error) });
    } finally {
      setBusy(false);
    }
  }

  async function applyCutover() {
    if (!preview) return;
    const values = await form.validateFields();
    const senders = accountIds(values.sender_pool_account_ids);
    if (!senders) {
      setNotice({ type: 'error', text: '发送账号池只能包含正整数账号 ID，且至少填写一个' });
      return;
    }
    setBusy(true);
    setNotice(null);
    try {
      const result = await api<{ clone_task_id: string }>(`/tasks/${props.task.id}/group-clone/cutover/apply`, {
        method: 'POST', body: JSON.stringify(cutoverRequest({ preview, values, senders, legacyName: props.task.name })),
      });
      setNotice({ type: 'success', text: `已完成原子切换，旧监听转发已暂停；Clone 任务 ${result.clone_task_id}` });
      props.onChanged();
    } catch (error) {
      setNotice({ type: 'error', text: errorMessage(error) });
    } finally {
      setBusy(false);
    }
  }

  return { form, preview, busy, notice, loadPreview, applyCutover };
}

export function GroupCloneCutoverPanel(props: CutoverProps) {
  const controller = useCutoverPanel(props);
  return <CutoverPanelView {...props} {...controller} />;
}

function CutoverPanelView(props: CutoverProps & ReturnType<typeof useCutoverPanel>) {
  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Alert type="warning" showIcon message="切换要求旧任务没有未收口 Action，并会在同一事务冻结 Telegram 起始边界和写权。" />
      {props.notice && <Alert type={props.notice.type} showIcon message={props.notice.text} />}
      {!props.preview ? <Button type="primary" disabled={!props.canManageTasks} loading={props.busy} onClick={() => void props.loadPreview()}>预检切换到 1 对 1 群克隆</Button> : (
        <>
          <Descriptions bordered size="small" column={2} items={[
            { key: 'source', label: '源群', children: props.preview.route_manifest.source_peer_id },
            { key: 'target', label: '目标群', children: props.preview.route_manifest.target_peer_id },
            { key: 'revision', label: '旧任务 revision', children: props.preview.expected_legacy_revision },
            { key: 'authority', label: '写权 version', children: props.preview.expected_authority_version },
          ]} />
          <CutoverForm form={props.form} />
          <Space>
            <Button disabled={props.busy} onClick={() => void props.loadPreview()}>重新预检</Button>
            <Button type="primary" danger loading={props.busy} disabled={!props.canManageTasks} onClick={() => void props.applyCutover()}>确认原子切换</Button>
          </Space>
        </>
      )}
    </Space>
  );
}

function CutoverForm({ form }: { form: FormInstance<CutoverFields> }) {
  const numberField = (name: keyof CutoverFields, label: string) => (
    <Form.Item name={name} label={label} rules={[{ required: true }]}><InputNumber min={1} precision={0} style={{ width: '100%' }} /></Form.Item>
  );
  return <Form form={form} layout="vertical" initialValues={{ authorization_mode: 'admin_authorized', reason: '迁移为 1 对 1 群克隆' }}>
    <div className="form-grid">
      {numberField('source_internal_group_id', '源群内部 ID')}{numberField('source_operation_target_id', '源群运营目标 ID')}
      {numberField('listener_account_id', '监听账号 ID')}{numberField('listener_authorization_id', '监听 authorization ID')}
      <Form.Item name="authorization_mode" label="源群授权方式" rules={[{ required: true }]}><Select options={[{ value: 'public', label: '公开群' }, { value: 'owned', label: '自有群' }, { value: 'admin_authorized', label: '管理员授权' }]} /></Form.Item>
      {numberField('target_internal_group_id', '目标群内部 ID')}{numberField('target_operation_target_id', '目标群运营目标 ID')}
      {numberField('control_account_id', '目标控制账号 ID')}{numberField('control_authorization_id', '目标 control authorization ID')}
      <Form.Item name="sender_pool_account_ids" label="发送账号 ID（逗号或换行）" rules={[{ required: true }]}><Input.TextArea rows={2} /></Form.Item>
      {numberField('rule_set_id', '冻结规则集 ID')}{numberField('rule_set_version', '冻结规则版本')}
    </div>
    <Form.Item name="reason" label="切换原因" rules={[{ required: true }]}><Input maxLength={255} /></Form.Item>
  </Form>;
}

function cutoverRequest(options: {
  preview: CutoverPreview; values: CutoverFields; senders: number[]; legacyName: string;
}) {
  const { preview, values, senders, legacyName } = options;
  return {
    preview_token: preview.preview_token, legacy_task_id: preview.legacy_task_id,
    expected_legacy_revision: preview.expected_legacy_revision,
    route_manifest_hash: preview.route_manifest_hash,
    expected_authority_version: preview.expected_authority_version,
    open_action_fingerprint: preview.open_action_fingerprint,
    reason: values.reason, client_request_id: requestId('cutover-apply'),
    clone_config: {
      name: `${legacyName} · 1对1克隆`, client_request_id: requestId('cutover-clone'),
      source: { internal_group_id: values.source_internal_group_id, operation_target_id: values.source_operation_target_id, peer_type: preview.route_manifest.source_peer_type, peer_id: preview.route_manifest.source_peer_id, listener_account_id: values.listener_account_id, authorization_id: values.listener_authorization_id, authorization_mode: values.authorization_mode },
      target: { internal_group_id: values.target_internal_group_id, operation_target_id: values.target_operation_target_id, peer_type: preview.route_manifest.target_peer_type, peer_id: preview.route_manifest.target_peer_id, control_account_id: values.control_account_id, control_authorization_id: values.control_authorization_id },
      sender_pool: { account_ids: senders }, pacing: { min_delay_ms: 1000, max_delay_ms: 6000, strict_target_order: true },
      content: { rule_set_id: values.rule_set_id, rule_set_version: values.rule_set_version },
      lifecycle: { start_mode: 'start_from_now', failure_order_policy: 'fail_stop' },
    },
  };
}
