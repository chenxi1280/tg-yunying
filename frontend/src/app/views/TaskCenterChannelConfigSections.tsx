import React from 'react';
import { Alert, Checkbox, Collapse, Form, Input, InputNumber, Select, Space } from 'antd';
import type { Rule } from 'antd/es/form';

type ChannelCommentTypeConfigProps = {
  replyMinPerMessageRules: Rule[];
  ruleFields: React.ReactNode;
};

export function ChannelViewTypeConfig() {
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Alert type="info" showIcon message="频道浏览按帖子、日期和账号补量：同一天同一账号同一帖子只会规划一次浏览。" />
      <div className="form-grid">
        <Form.Item name="per_message_daily_view_target" label="每条帖子每日浏览量"><InputNumber min={1} max={10000} /></Form.Item>
        <Form.Item name="per_message_total_view_target" label="每条帖子累计目标"><InputNumber min={1} max={100000} /></Form.Item>
        <Form.Item name="listen_new_messages" valuePropName="checked">
          <Checkbox>持续监听任务启动后的新帖</Checkbox>
        </Form.Item>
      </div>
      <Collapse
        ghost
        items={[{ key: 'advanced', label: '高级设置', children: <ChannelViewAdvancedFields /> }]}
      />
    </Space>
  );
}

export function ChannelLikeTypeConfig() {
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <div className="form-grid">
        <Form.Item name="target_likes_per_message" label="预计每条点赞"><InputNumber min={1} /></Form.Item>
        <Form.Item name="allowed_reactions" label="Reaction 范围"><Input /></Form.Item>
      </div>
      <Collapse
        ghost
        items={[{ key: 'advanced', label: '高级设置', children: <ChannelLikeAdvancedFields /> }]}
      />
    </Space>
  );
}

export function ChannelCommentTypeConfig({ replyMinPerMessageRules, ruleFields }: ChannelCommentTypeConfigProps) {
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <div style={{ gridColumn: '1 / -1' }}>
        <Alert type="info" showIcon message="AI 评论会按绑定规则集逐条做输出校验，单条失败不会废弃整批评论。" />
        <Alert type="info" showIcon message="小时上限控制总量；每条评论/回复是累计目标，系统按差额补计划。" />
      </div>
      <div className="form-grid">
        <div style={{ gridColumn: '1 / -1' }}>{ruleFields}</div>
        <Form.Item name="target_comments_per_message" label="预计每条评论/回复"><InputNumber min={1} /></Form.Item>
        <Form.Item name="reply_min_per_message" label="每条最少引用回复数" dependencies={['target_comments_per_message']} rules={replyMinPerMessageRules}><InputNumber min={0} /></Form.Item>
        <Form.Item name="comment_style" label="评论方向"><Select options={[{ value: 'mixed', label: '混合' }, { value: 'relevant', label: '相关' }, { value: 'question', label: '提问' }, { value: 'praise', label: '正向' }, { value: 'discussion', label: '讨论' }]} /></Form.Item>
        <Form.Item name="topic_hint" label="主题方向"><Input /></Form.Item>
      </div>
      <Collapse
        ghost
        items={[{ key: 'advanced', label: '高级设置', children: <ChannelCommentAdvancedFields /> }]}
      />
    </Space>
  );
}

function ChannelViewAdvancedFields() {
  return (
    <div className="form-grid">
      <Form.Item name="message_active_days" label="帖子有效期（天）"><InputNumber min={1} max={365} /></Form.Item>
      <Form.Item name="task_daily_view_safety_cap" label="任务每日安全上限"><InputNumber min={1} max={100000} /></Form.Item>
      <Form.Item name="max_views_per_account_per_day" label="每号每日浏览上限"><InputNumber min={1} max={10000} /></Form.Item>
      <Form.Item name="view_count_jitter" label="浏览量随机抖动"><InputNumber min={0} max={1} step={0.01} /></Form.Item>
      <Form.Item name="execution_mode" label="执行模式"><Select options={[{ value: 'distribute', label: '均匀分配' }, { value: 'burst', label: '尽快完成' }]} /></Form.Item>
    </div>
  );
}

function ChannelLikeAdvancedFields() {
  return (
    <div className="form-grid">
      <Form.Item name="max_likes_per_account_per_hour" label="每号每小时点赞上限"><InputNumber min={1} /></Form.Item>
      <Form.Item name="like_count_jitter" label="点赞量随机抖动"><InputNumber min={0} max={1} step={0.01} /></Form.Item>
    </div>
  );
}

function ChannelCommentAdvancedFields() {
  return (
    <div className="form-grid">
      <Form.Item name="max_comments_per_account_per_hour" label="每号每小时评论上限"><InputNumber min={1} /></Form.Item>
      <Form.Item name="system_prompt_override" label="System Prompt 覆盖"><Input.TextArea rows={3} /></Form.Item>
      <Form.Item name="max_comment_length" label="最大评论长度"><InputNumber min={1} /></Form.Item>
    </div>
  );
}
