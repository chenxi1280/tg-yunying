import React from 'react';
import { Space, Typography } from 'antd';

const PHASES: Record<string, string> = {
  resolve_target: '解析目标', prepare_send: '发送前准备', send_call: '发送调用', receipt_received: '取得回执后',
};
const MUTATIONS: Record<string, string> = { true: '已发生', false: '未发生', unknown: '未知' };
const REJECTIONS: Record<string, string> = {
  post_send_control_source_untrusted_non_bot: '非机器人来源',
  post_send_control_source_untrusted_non_admin: '非管理员来源',
  post_send_control_source_role_lookup_failed: '来源身份查询失败',
  post_send_control_source_role_unknown: '来源身份未知',
  post_send_control_semantics_missing: '未识别为验证提示',
  post_send_control_callback_missing: '缺少确认按钮',
};

export function TaskActionFailureEvidence({ result }: { result?: Record<string, any> | null }) {
  const send = result?.send_diagnostics;
  const control = result?.post_send_control_diagnostics;
  if (!send && !control) return <>-</>;
  return <Space direction="vertical" size={2}>
    {send && <>
      <Typography.Text>{PHASES[send.failure_stage] || send.failure_stage}：{send.rpc_error_type}</Typography.Text>
      <Typography.Text type="secondary">远端修改：{MUTATIONS[send.remote_mutation_state] || send.remote_mutation_state}</Typography.Text>
    </>}
    {control?.read_status === 'failed' && <Typography.Text>读取失败：{control.error_type}</Typography.Text>}
    {control?.read_status === 'observed' && <>
      <Typography.Text>读取 {control.raw_message_count} 条，按钮候选 {control.candidate_message_count} 条</Typography.Text>
      {Object.entries(control.rejection_counts || {}).map(([reason, count]) => (
        <Typography.Text key={reason} type="secondary">{REJECTIONS[reason] || reason}：{String(count)} 条</Typography.Text>
      ))}
      {Object.entries(control.source_role_error_counts || {}).map(([reason, count]) => (
        <Typography.Text key={reason} type="secondary">身份查询 {reason}：{String(count)} 次</Typography.Text>
      ))}
      {control.limit_reached && <Typography.Text type="secondary">读取已达到窗口上限</Typography.Text>}
    </>}
  </Space>;
}
