import React from 'react';
import { Alert, Checkbox, Collapse, Form, Input, InputNumber, Select, Space } from 'antd';
import type { Rule } from 'antd/es/form';
import { aiModelIdentity } from './taskCenterViewModel';

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
        <Form.Item
          name="per_message_total_view_target"
          label="每条帖子累计目标"
          extra="填写 0 表示无累计上限；有限目标允许按当日批次粒度超额。"
        >
          <InputNumber min={0} max={100000} />
        </Form.Item>
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
      <Alert type="info" showIcon message="默认从频道当前全部可用标准 Reaction 中逐个随机分配，各表情数量不设固定比例。" />
      <div className="form-grid">
        <Form.Item name="target_likes_per_message" label="预计每条点赞"><InputNumber min={1} /></Form.Item>
        <Form.Item name="reaction_type" label="Reaction 模式"><Select options={[{ value: 'random', label: '随机' }, { value: 'specific', label: '指定' }]} /></Form.Item>
        <Form.Item name="reaction_scope" label="随机范围"><Select options={[{ value: 'all_available', label: '频道可用全部 Reaction' }, { value: 'configured', label: '自定义 Reaction' }]} /></Form.Item>
        <Form.Item name="allowed_reactions" label="自定义/指定 Reaction"><Input /></Form.Item>
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
        <Alert type="info" showIcon message="任务总评论上限控制生命周期总量；每条评论/回复是单条消息累计目标；小时上限只控制发送节奏。" />
      </div>
      <div className="form-grid">
        <div style={{ gridColumn: '1 / -1' }}>{ruleFields}</div>
        <Form.Item name="max_total_comments" label="系统任务门禁（固定）"><InputNumber min={1000000} max={1000000} disabled /></Form.Item>
        <Form.Item name="target_comments_per_message" label="预计每条评论/回复"><InputNumber min={1} /></Form.Item>
        <Form.Item name="reply_min_per_message" label="每条最少引用回复数" dependencies={['target_comments_per_message']} rules={replyMinPerMessageRules}><InputNumber min={0} /></Form.Item>
        <Form.Item name="daily_comment_cap" label="每日评论上限" extra="0 表示不限制每日额度"><InputNumber min={0} /></Form.Item>
        <Form.Item name="rolling_window_days" label="滚动排期窗口（天）" extra="单帖在指定天数内平滑排期（默认 1 天）"><InputNumber min={1} max={30} /></Form.Item>
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
      <Form.Item name="task_daily_view_safety_cap" label="系统任务门禁（固定）"><InputNumber min={1000000} max={1000000} disabled /></Form.Item>
      <Form.Item name="max_views_per_account_per_day" label="系统账号门禁（固定）"><InputNumber min={1000000} max={1000000} disabled /></Form.Item>
      <Form.Item name="view_count_jitter" label="浏览量随机抖动"><InputNumber min={0} max={1} step={0.01} /></Form.Item>
      <Form.Item name="execution_mode" label="执行模式"><Select options={[{ value: 'distribute', label: '均匀分配' }, { value: 'burst', label: '尽快完成' }]} /></Form.Item>
    </div>
  );
}

function ChannelLikeAdvancedFields() {
  return (
    <div className="form-grid">
      <Form.Item name="max_likes_per_account_per_hour" label="系统账号门禁（固定）"><InputNumber min={1000000} max={1000000} disabled /></Form.Item>
      <Form.Item name="like_count_jitter" label="点赞量随机抖动"><InputNumber min={0} max={1} step={0.01} /></Form.Item>
    </div>
  );
}

function ChannelCommentAdvancedFields() {
  return (
    <div className="form-grid">
      <Form.Item name="max_comments_per_account_per_hour" label="系统账号门禁（固定）"><InputNumber min={1000000} max={1000000} disabled /></Form.Item>
      <Form.Item name="max_total_comments_jitter" label="任务门禁抖动（固定）"><InputNumber min={0} max={0} disabled /></Form.Item>
      <Form.Item name="comment_count_jitter" label="评论数量抖动"><InputNumber min={0} max={1} step={0.01} /></Form.Item>
      <Form.Item name="system_prompt_override" label="System Prompt 覆盖"><Input.TextArea rows={3} /></Form.Item>
      <Form.Item name="max_comment_length" label="最大评论长度"><InputNumber min={1} /></Form.Item>
      <Form.Item
        name="ai_model"
        label="生成模型"
        dependencies={['ai_two_stage_enabled']}
        extra="启用两阶段生成时必须显式配置，以保证与评审模型不同。"
        rules={[
          ({ getFieldValue }: any) => ({
            validator(_: unknown, value?: string) {
              if (!getFieldValue('ai_two_stage_enabled') || String(value || '').trim()) return Promise.resolve();
              return Promise.reject(new Error('请显式配置生成模型'));
            },
          }),
        ]}
      >
        <Input placeholder="留空使用租户默认生成模型" />
      </Form.Item>
      <Form.Item name="ai_two_stage_enabled" label="两阶段生成" valuePropName="checked">
        <Checkbox>启用意图规划、独立表达与语义质量评审</Checkbox>
      </Form.Item>
      <Form.Item
        name="ai_semantic_reviewer_model"
        label="独立语义评审模型"
        dependencies={['ai_two_stage_enabled', 'ai_model']}
        extra="启用两阶段生成时必填，且不能与生成模型相同。"
        rules={[
          ({ getFieldValue }: any) => ({
            validator(_: unknown, value?: string) {
              if (!getFieldValue('ai_two_stage_enabled')) return Promise.resolve();
              const reviewer = String(value || '').trim();
              if (!reviewer) return Promise.reject(new Error('请配置独立语义评审模型'));
              if (aiModelIdentity(reviewer) === aiModelIdentity(getFieldValue('ai_model'))) {
                return Promise.reject(new Error('语义评审模型不能与生成模型相同'));
              }
              return Promise.resolve();
            },
          }),
        ]}
      >
        <Input placeholder="例如与生成模型不同的已配置模型 ID" />
      </Form.Item>
      <Form.Item name="ai_content_route_v2_enabled" label="内容路由 v2" valuePropName="checked">
        <Checkbox>绑定已审批策略、Provider 顺序与成人主题证明</Checkbox>
      </Form.Item>
      <Form.Item name="ai_content_policy_version_id" label="内容策略版本 ID" dependencies={['ai_content_route_v2_enabled']} rules={[({ getFieldValue }: any) => ({ required: Boolean(getFieldValue('ai_content_route_v2_enabled')), message: '请填写已激活策略版本 ID' })]}>
        <Input />
      </Form.Item>
      <Form.Item name="ai_content_allowed_routes" label="允许内容路由" dependencies={['ai_content_route_v2_enabled']}>
        <Select mode="multiple" options={['general', 'adult_visual', 'adult_product', 'adult_service_inquiry', 'adult_service_sensory'].map((value) => ({ value, label: value }))} />
      </Form.Item>
      <Form.Item name="ai_content_attestation_ids" label="成人主题证明 ID">
        <Select mode="tags" tokenSeparators={[',', '，']} placeholder="仅成人路由需要" />
      </Form.Item>
    </div>
  );
}
