import React from 'react';
import { Form, Input, InputNumber, Select } from 'antd';

import OperationTargetSelect from '../components/OperationTargetSelect';
import type { ChannelMessage, OperationTarget, TaskCenterTaskType } from '../types';

const messageSelectProps = {
  showSearch: true,
  optionFilterProp: 'label' as const,
  filterOption: (input: string, option?: { label?: unknown }) => String(option?.label ?? '').toLowerCase().includes(input.trim().toLowerCase()),
};

type WizardTargetProps = Readonly<{
  taskType: TaskCenterTaskType;
  messages: ChannelMessage[];
  messageScope: string;
  targetChannelId?: number;
  onTargetChannelChange: () => void;
  onTargetsLoaded?: (targets: readonly OperationTarget[]) => void;
  allowInlineTarget?: boolean;
  simpleSearchCreation?: boolean;
}>;

type GroupTargetFieldProps = Readonly<{
  name: string;
  label: string;
  mode?: 'multiple';
  capability: 'task' | 'send' | 'listen';
  required?: boolean;
  onTargetsLoaded?: (targets: readonly OperationTarget[]) => void;
}>;

function GroupTargetField({ name, label, mode, capability, required, onTargetsLoaded }: GroupTargetFieldProps) {
  return (
    <Form.Item name={name} label={label} rules={required ? [{ required: true }] : undefined}>
      <OperationTargetSelect mode={mode} allowClear query={{ targetType: 'group', capability }} onTargetsLoaded={onTargetsLoaded} />
    </Form.Item>
  );
}

function GroupTaskTargetFields({ taskType, onTargetsLoaded, allowInlineTarget }: Pick<WizardTargetProps, 'taskType' | 'onTargetsLoaded' | 'allowInlineTarget'>) {
  const searchClickTask = taskType === 'search_join_group' || taskType === 'search_rank_deboost';
  return (
    <div className="form-grid">
      <GroupTargetField name="target_operation_target_id" label={searchClickTask ? '目标群' : '已有运营目标群'} capability="task" required={taskType !== 'group_ai_chat'} onTargetsLoaded={onTargetsLoaded} />
      {taskType === 'group_ai_chat' && allowInlineTarget && <Form.Item name="target_input" label="粘贴新群入口"><Input placeholder="@group_name / https://t.me/+invite / peer id" /></Form.Item>}
      {taskType === 'group_ai_chat' && allowInlineTarget && <Form.Item name="target_title" label="目标名称"><Input placeholder="可选，不填时使用入口作为名称" /></Form.Item>}
    </div>
  );
}

function RelayTargetFields({ onTargetsLoaded, allowInlineTarget }: Pick<WizardTargetProps, 'onTargetsLoaded' | 'allowInlineTarget'>) {
  return (
    <div className="form-grid">
      <GroupTargetField name="source_operation_target_ids" label="源群运营目标" mode="multiple" capability="listen" onTargetsLoaded={onTargetsLoaded} />
      {allowInlineTarget && <Form.Item name="source_target_input" label="粘贴新源群入口"><Input placeholder="@source_group / 邀请链接" /></Form.Item>}
      <GroupTargetField name="target_operation_target_id" label="默认目标群" capability="send" onTargetsLoaded={onTargetsLoaded} />
      {allowInlineTarget && <Form.Item name="target_input" label="粘贴新目标群入口"><Input placeholder="@target_group / 邀请链接" /></Form.Item>}
      <GroupTargetField name="target_operation_target_ids" label="附加目标群" mode="multiple" capability="send" onTargetsLoaded={onTargetsLoaded} />
    </div>
  );
}

function ChannelTargetFields(props: WizardTargetProps) {
  const scopedMessages = props.messages.filter((message) => !props.targetChannelId || message.channel_target_id === props.targetChannelId);
  return (
    <div className="form-grid">
      <Form.Item name="target_channel_id" label="已有目标频道">
        <OperationTargetSelect allowClear query={{ targetType: 'channel', capability: 'task' }} onChange={props.onTargetChannelChange} onTargetsLoaded={props.onTargetsLoaded} />
      </Form.Item>
      {props.allowInlineTarget && <Form.Item name="target_input" label="粘贴新频道入口"><Input placeholder="@channel / https://t.me/channel / https://t.me/+invite" /></Form.Item>}
      {props.allowInlineTarget && <Form.Item name="target_title" label="频道名称"><Input placeholder="可选，不填时使用入口作为名称" /></Form.Item>}
      <Form.Item name="message_scope" label="消息范围"><Select options={[{ value: 'dynamic_new', label: '持续监听新消息' }, { value: 'latest_n', label: '最新 N 条' }, { value: 'all', label: '所有消息' }, { value: 'date_range', label: '日期范围' }, { value: 'specific', label: '指定消息' }]} /></Form.Item>
      {['latest_n', 'dynamic_new'].includes(props.messageScope) && <Form.Item name="message_count" label={props.messageScope === 'dynamic_new' ? '每轮采集上限' : '消息数量'} rules={[{ required: true }]}><InputNumber min={1} max={500} /></Form.Item>}
      {props.messageScope === 'specific' && <Form.Item name="message_ids" label="频道消息" rules={[{ required: true }]}><Select mode="multiple" options={scopedMessages.map((message) => ({ value: message.id, label: `#${message.message_id} / ${message.content_preview || message.message_url || message.id}` }))} {...messageSelectProps} /></Form.Item>}
      {props.messageScope === 'date_range' && <><Form.Item name="date_from" label="开始时间"><Input type="datetime-local" /></Form.Item><Form.Item name="date_to" label="结束时间"><Input type="datetime-local" /></Form.Item></>}
    </div>
  );
}

export function WizardTarget(props: WizardTargetProps) {
  const normalizedProps = { ...props, allowInlineTarget: props.allowInlineTarget ?? true };
  if (['group_ai_chat', 'group_membership_admission', 'search_join_group'].includes(props.taskType) || (props.taskType === 'search_rank_deboost' && props.simpleSearchCreation)) {
    return <GroupTaskTargetFields taskType={props.taskType} onTargetsLoaded={props.onTargetsLoaded} allowInlineTarget={normalizedProps.allowInlineTarget} />;
  }
  if (props.taskType === 'search_rank_deboost') {
    return <div className="form-grid"><GroupTargetField name="target_group_ids" label="我方目标群（可多选）" mode="multiple" capability="task" required onTargetsLoaded={props.onTargetsLoaded} /></div>;
  }
  if (props.taskType === 'group_relay') {
    return <RelayTargetFields onTargetsLoaded={props.onTargetsLoaded} allowInlineTarget={normalizedProps.allowInlineTarget} />;
  }
  return <ChannelTargetFields {...normalizedProps} />;
}
