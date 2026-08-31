import React from 'react';
import { Alert, Descriptions, Form, Input, InputNumber, Select, Space } from 'antd';

export function GroupCloneTaskReview({ values }: { values: Record<string, any> }) {
  const senderIds = Array.isArray(values.sender_pool_account_ids)
    ? values.sender_pool_account_ids.join('、')
    : String(values.sender_pool_account_ids || '').replace(/[,，\n\s]+/g, '、');
  return <Descriptions bordered column={2} size="small" items={[
    { key: 'source', label: '源群', children: `运营目标 #${values.source_operation_target_id || '-'} / ${values.source_peer_id || '-'}` },
    { key: 'target', label: '克隆目标群', children: `运营目标 #${values.target_operation_target_id || '-'} / ${values.target_peer_id || '-'}` },
    { key: 'listener', label: '监听授权', children: `账号 #${values.listener_account_id || '-'} / authorization #${values.listener_authorization_id || '-'}` },
    { key: 'control', label: '目标控制授权', children: `账号 #${values.control_account_id || '-'} / authorization #${values.control_authorization_id || '-'}` },
    { key: 'senders', label: '发送账号池', children: senderIds || '-' },
    { key: 'binding', label: '绑定阶段', children: `${values.active_minutes ?? 30}/${values.guarded_minutes ?? 120}/${values.eligible_release_minutes ?? 720} 分钟；最短占用 ${values.minimum_tenure_minutes ?? 60} 分钟` },
    { key: 'rule', label: '冻结内容规则', children: `规则集 #${values.rule_set_id || '-'} / 版本 ${values.rule_set_version || '-'}` },
    { key: 'pacing', label: '发送节奏', children: `${values.min_delay_ms ?? 1000}–${values.max_delay_ms ?? 6000} ms，严格保序` },
    { key: 'failure', label: '失败策略', children: `${values.failure_order_policy || 'fail_stop'} / unknown ${values.unknown_deadline_seconds ?? 900}s` },
    { key: 'precheck', label: '启动前检查', children: '提交时执行实时权限、Update Ingress、route cycle 与独占写权检查；hard block 会阻止创建' },
  ]} />;
}

export function GroupCloneTaskFields({ editing = false }: { editing?: boolean }) {
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Alert
        type="warning"
        showIcon
        message={editing
          ? '发送账号池已冻结；调整账号必须走受控换绑流程。其余策略只影响新采集的源事件。'
          : '目标群使用独占写权；启动前会校验共享 Update Ingress、授权 generation 和发送权限。'}
      />
      <div className="form-grid">
        {!editing && <><Form.Item name="listener_account_id" label="源群 listener 账号 ID" rules={[{ required: true }]}><InputNumber min={1} precision={0} style={{ width: '100%' }} /></Form.Item>
          <Form.Item name="listener_authorization_id" label="listener authorization ID" rules={[{ required: true }]}><InputNumber min={1} precision={0} style={{ width: '100%' }} /></Form.Item>
          <Form.Item name="authorization_mode" label="源群授权方式" rules={[{ required: true }]}><Select options={[{ value: 'public', label: '公开群' }, { value: 'owned', label: '自有群' }, { value: 'admin_authorized', label: '管理员授权' }]} /></Form.Item>
          <Form.Item name="control_account_id" label="目标控制账号 ID" rules={[{ required: true }]}><InputNumber min={1} precision={0} style={{ width: '100%' }} /></Form.Item>
          <Form.Item name="control_authorization_id" label="control authorization ID" rules={[{ required: true }]}><InputNumber min={1} precision={0} style={{ width: '100%' }} /></Form.Item></>}
        <Form.Item name="sender_pool_account_ids" label="发送账号 ID（逗号或换行）" rules={[{ required: true }]}><Input.TextArea rows={3} disabled={editing} /></Form.Item>
        <Form.Item name="active_minutes" label="活跃绑定分钟"><InputNumber min={1} precision={0} style={{ width: '100%' }} /></Form.Item>
        <Form.Item name="guarded_minutes" label="保护绑定分钟"><InputNumber min={1} precision={0} style={{ width: '100%' }} /></Form.Item>
        <Form.Item name="eligible_release_minutes" label="可释放分钟"><InputNumber min={1} precision={0} style={{ width: '100%' }} /></Form.Item>
        <Form.Item name="minimum_tenure_minutes" label="最短占用分钟"><InputNumber min={1} precision={0} style={{ width: '100%' }} /></Form.Item>
        <Form.Item name="rule_set_id" label="冻结规则集 ID" rules={[{ required: true }]}><InputNumber min={1} precision={0} style={{ width: '100%' }} /></Form.Item>
        <Form.Item name="rule_set_version" label="冻结规则版本号" rules={[{ required: true }]}><InputNumber min={1} precision={0} style={{ width: '100%' }} /></Form.Item>
        <Form.Item name="min_delay_ms" label="最小发送延迟 ms" rules={[{ required: true }]}><InputNumber min={0} precision={0} style={{ width: '100%' }} /></Form.Item>
        <Form.Item name="max_delay_ms" label="最大发送延迟 ms" rules={[{ required: true }]}><InputNumber min={0} precision={0} style={{ width: '100%' }} /></Form.Item>
        <Form.Item name="orphan_reply_policy" label="孤儿回复策略"><Select options={[{ value: 'quote_fallback', label: '引用降级' }, { value: 'drop_subtree', label: '丢弃子树' }, { value: 'block_for_review', label: '人工审核' }]} /></Form.Item>
        <Form.Item name="incomplete_album_policy" label="不完整相册策略"><Select options={[{ value: 'drop_incomplete', label: '整组丢弃' }, { value: 'send_partial_degraded', label: '降级发送部分' }]} /></Form.Item>
        <Form.Item name="unsupported_media_policy" label="不支持媒体策略"><Select options={[{ value: 'block', label: '阻断' }, { value: 'manual_review', label: '人工审核' }]} /></Form.Item>
        <Form.Item name="failure_order_policy" label="失败保序策略"><Select options={[{ value: 'fail_stop', label: '阻断并等待决策' }, { value: 'continue_with_visible_gap', label: '记录可见缺口后继续' }]} /></Form.Item>
        <Form.Item name="unknown_deadline_seconds" label="未知结果截止秒"><InputNumber min={60} precision={0} style={{ width: '100%' }} /></Form.Item>
        <Form.Item name="source_event_days" label="源事件保留天数"><InputNumber min={1} precision={0} style={{ width: '100%' }} /></Form.Item>
        <Form.Item name="media_cache_ttl_seconds" label="媒体缓存秒"><InputNumber min={60} precision={0} style={{ width: '100%' }} /></Form.Item>
      </div>
    </Space>
  );
}
