import React from 'react';
import { Alert, Checkbox, Collapse, Form, Input, InputNumber, Select, Space } from 'antd';
import type { Rule } from 'antd/es/form';
import type { MaterialGroup } from '../types';
import { aiModelIdentity } from './taskCenterViewModel';

type ChannelCommentTypeConfigProps = {
  replyMinPerMessageRules: Rule[];
  ruleFields: React.ReactNode;
  materialGroups: MaterialGroup[];
};

export function ChannelViewTypeConfig() {
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Alert type="info" showIcon message="频道浏览按帖子、日期和账号补量：同一天同一账号同一帖子只会规划一次浏览。" />
      <div className="form-grid">
        <Form.Item
          name="per_message_daily_view_target"
          label="每条帖子每日浏览量"
          extra="留空默认自动使用全部可用账号进行全量覆盖"
        >
          <InputNumber min={1} max={10000} placeholder="全部可用账号" />
        </Form.Item>
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
        <Form.Item name="allowed_reactions" label="自定义/指定 Reaction" extra="多个表情用逗号分隔，如：👍, ❤️, 🔥, 👏, 🎉, 🤩, 👌, 💯, 🙌, ✨"><Input placeholder="👍, ❤️, 🔥, 👏, 🎉, 🤩, 👌, 💯, 🙌, ✨" /></Form.Item>
      </div>
      <Collapse
        ghost
        items={[{ key: 'advanced', label: '高级设置', children: <ChannelLikeAdvancedFields /> }]}
      />
    </Space>
  );
}

export function ChannelCommentTypeConfig({ replyMinPerMessageRules, ruleFields, materialGroups }: ChannelCommentTypeConfigProps) {
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <div style={{ gridColumn: '1 / -1' }}>
        <Alert type="info" showIcon message="AI 评论会按绑定规则集逐条做输出校验，单条失败不会废弃整批评论。" />
        <Alert type="info" showIcon message="任务总评论上限控制生命周期总量；每条评论/回复是单条消息累计目标；小时上限只控制发送节奏。" />
        <Alert type="warning" showIcon message="新版相关性合同不会把回复缺口静默改成顶层评论；计划兜底超过上限或落到回复槽时会阻断该帖。" />
      </div>
      <div className="form-grid">
        <div style={{ gridColumn: '1 / -1' }}>{ruleFields}</div>
        <Form.Item name="max_total_comments" label="系统任务门禁（固定）"><InputNumber min={1000000} max={1000000} disabled /></Form.Item>
        <Form.Item name="target_comments_per_message" label="预计每条评论/回复"><InputNumber min={1} /></Form.Item>
        <Form.Item name="business_max_comments_per_message" label="单帖业务评论上限" extra="55%～65% 需求量超过此值时按上限冻结，并显示业务 cap 已调整。"><InputNumber min={1} max={1000} /></Form.Item>
        <Form.Item name="planned_fallback_max_bps" label="计划兜底上限（万分比）" extra="2000 表示最多 20%；兜底只允许用于顶层评论。"><InputNumber min={0} max={10000} step={100} /></Form.Item>
        <Form.Item name="comment_mode" label="评论关系模式">
          <Select options={[{ value: 'comment', label: '仅顶层评论' }, { value: 'mixed', label: '顶层评论 + 引用回复' }, { value: 'reply', label: '仅引用回复' }]} />
        </Form.Item>
        <Form.Item
          name="reply_to_message_ids"
          label="指定回复目标 ID"
          dependencies={['comment_mode']}
          extra="仅引用回复模式必填；多个 ID 用逗号分隔。混合模式留空时从已采集根评论中优先选择未回答问题。"
          rules={[({ getFieldValue }) => ({
            validator: async (_, value) => {
              const hasValue = Array.isArray(value) ? value.length > 0 : String(value || '').trim().length > 0;
              if (getFieldValue('comment_mode') !== 'reply' || hasValue) return;
              throw new Error('仅引用回复模式必须指定回复目标 ID');
            },
          })]}
        >
          <Input placeholder="例如 12345, 12346" />
        </Form.Item>
        <Form.Item name="reply_min_per_message" label="每条最少引用回复数" dependencies={['target_comments_per_message']} rules={replyMinPerMessageRules}><InputNumber min={0} /></Form.Item>
        <Form.Item
          name="daily_comment_cap"
          label="每日评论上限"
          dependencies={['channel_comment_grounding_v1_enabled']}
          extra="启用频道评论 grounding v1 时必须填写正整数"
          rules={[({ getFieldValue }) => ({
            validator: async (_, value) => {
              if (!getFieldValue('channel_comment_grounding_v1_enabled') || Number(value) > 0) return;
              throw new Error('启用频道评论 grounding v1 时，每日评论上限必须大于 0');
            },
          })]}
        >
          <InputNumber min={0} />
        </Form.Item>
        <Form.Item name="rolling_window_days" label="滚动排期窗口（天）" extra="新合同固定从 Telegram 发布时间起 3 天"><InputNumber min={3} max={3} disabled /></Form.Item>
        <Form.Item name="comment_style" label="评论方向"><Select options={[{ value: 'mixed', label: '混合' }, { value: 'relevant', label: '相关' }, { value: 'question', label: '提问' }, { value: 'praise', label: '正向' }, { value: 'discussion', label: '讨论' }]} /></Form.Item>
        <Form.Item name="topic_hint" label="主题方向"><Input /></Form.Item>
      </div>
      <Collapse
        ghost
        items={[{ key: 'advanced', label: '高级设置', children: <ChannelCommentAdvancedFields materialGroups={materialGroups} /> }]}
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
      <Form.Item name="message_active_days" label="帖子有效期（天）"><InputNumber min={1} max={365} /></Form.Item>
      <Form.Item name="max_likes_per_account_per_hour" label="系统账号门禁（固定）"><InputNumber min={1000000} max={1000000} disabled /></Form.Item>
      <Form.Item name="like_count_jitter" label="点赞量随机抖动"><InputNumber min={0} max={1} step={0.01} /></Form.Item>
    </div>
  );
}

function ChannelCommentAdvancedFields({ materialGroups }: { materialGroups: MaterialGroup[] }) {
  const imageMemeGroups = materialGroups.filter(
    (group) => group.is_active && group.ready_image_meme_count > 0,
  );
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
      <Form.Item name="channel_comment_grounding_v1_enabled" label="频道评论相关性 v1" valuePropName="checked">
        <Checkbox>启用证据化评论和 v2 表情兜底合同</Checkbox>
      </Form.Item>
      <Form.Item name="auto_join_discussion_enabled" label="自动加入讨论组" valuePropName="checked">
        <Checkbox>仅对下方精确授权账号创建独立 Join Action（默认关闭）</Checkbox>
      </Form.Item>
      <Form.Item
        name="discussion_join_account_ids"
        label="允许自动入组账号 ID"
        dependencies={['auto_join_discussion_enabled']}
        rules={[({ getFieldValue }) => ({
          required: Boolean(getFieldValue('auto_join_discussion_enabled')),
          message: '开启自动入组时必须填写精确账号 ID',
        })]}
      >
        <Select mode="tags" tokenSeparators={[',', '，']} placeholder="例如 101, 102" />
      </Form.Item>
      <Form.Item
        name="discussion_join_budget"
        label="单次入组预算"
        dependencies={['auto_join_discussion_enabled']}
        rules={[({ getFieldValue }) => ({
          validator: async (_, value) => {
            if (!getFieldValue('auto_join_discussion_enabled') || Number(value) > 0) return;
            throw new Error('开启自动入组时预算必须大于 0');
          },
        })]}
      >
        <InputNumber min={0} />
      </Form.Item>
      <Form.Item
        name="discussion_join_pacing_policy_version"
        label="入组节奏协议版本"
        dependencies={['auto_join_discussion_enabled']}
        rules={[({ getFieldValue }) => ({
          required: Boolean(getFieldValue('auto_join_discussion_enabled')),
          message: '开启自动入组时必须冻结节奏协议版本',
        })]}
      >
        <Input readOnly />
      </Form.Item>
      <Form.Item
        name={['discussion_join_pacing_policy', 'interval_seconds']}
        label="入组动作间隔（秒）"
        dependencies={['auto_join_discussion_enabled']}
        rules={[({ getFieldValue }) => ({
          validator: async (_, value) => {
            if (!getFieldValue('auto_join_discussion_enabled') || Number(value) > 0) return;
            throw new Error('开启自动入组时必须配置正数动作间隔');
          },
        })]}
      >
        <InputNumber min={1} />
      </Form.Item>
      <Alert type="warning" showIcon message="自动入组会产生真实 Telegram 外部变更；关闭时，未具备 fresh 讨论组成员事实的账号只会阻塞，不会静默加群。" />
      <Form.Item label="Unicode 表情白名单">
        <Input readOnly value="👍 🙂 👏 🔥 ❤️ 😍 🤩 🎉 💯 🙌 👌 ✨ 😄 😊 🥳 👀 🤝 💪 🌟 💖" />
      </Form.Item>
      <Form.Item name="unicode_emoji_enabled" label="文字表情兜底" valuePropName="checked">
        <Checkbox>允许 20 个白名单 Unicode 表情</Checkbox>
      </Form.Item>
      <Form.Item name="unicode_emoji_weight_bps" label="文字表情权重（bps）" dependencies={fallbackPolicyFields} rules={fallbackWeightRules()}><InputNumber min={0} max={10000} /></Form.Item>
      <Form.Item name="image_meme_enabled" label="图片表情包兜底" valuePropName="checked">
        <Checkbox>允许静态 image_meme 素材</Checkbox>
      </Form.Item>
      <Form.Item name="image_meme_material_group_id" label="图片表情包素材组" dependencies={['image_meme_enabled', 'image_meme_weight_bps']} rules={[({ getFieldValue }: any) => ({ required: Boolean(getFieldValue('image_meme_enabled')) && Number(getFieldValue('image_meme_weight_bps') || 0) > 0, message: '图片权重大于 0 时请选择有 ready 素材的图片表情包组' })]}>
        <Select
          allowClear
          options={imageMemeGroups.map((group) => ({
            value: group.id,
            label: `${group.name}（ready ${group.ready_image_meme_count}）`,
          }))}
          placeholder="只显示有 ready 静态 image_meme 的素材组"
        />
      </Form.Item>
      <Form.Item noStyle dependencies={['image_meme_material_group_id']}>
        {({ getFieldValue }) => {
          const group = materialGroups.find((item) => item.id === getFieldValue('image_meme_material_group_id'));
          return group ? <Alert type="info" showIcon message={`当前预览 ${group.ready_image_meme_count} 个 ready 版本，hash ${group.ready_image_meme_pool_hash.slice(0, 12)}…；每条消息首次规划时才冻结真实池。`} /> : null;
        }}
      </Form.Item>
      <Form.Item name="image_meme_weight_bps" label="图片表情包权重（bps）" dependencies={fallbackPolicyFields} rules={fallbackWeightRules()}><InputNumber min={0} max={10000} /></Form.Item>
      <Form.Item name="allow_image_reselection_before_gateway" valuePropName="checked">
        <Checkbox>图片在 Gateway 前失效时顺延冻结池下一张</Checkbox>
      </Form.Item>
      <Form.Item name="allow_cross_kind_fallback_to_unicode" valuePropName="checked">
        <Checkbox>图片冻结池耗尽时允许转 Unicode 表情</Checkbox>
      </Form.Item>
    </div>
  );
}

const fallbackPolicyFields = [
  'channel_comment_grounding_v1_enabled',
  'unicode_emoji_enabled',
  'image_meme_enabled',
  'unicode_emoji_weight_bps',
  'image_meme_weight_bps',
];

function fallbackWeightRules(): Rule[] {
  return [
    ({ getFieldValue }: any) => ({
      validator() {
        if (!getFieldValue('channel_comment_grounding_v1_enabled')) return Promise.resolve();
        const unicodeEnabled = Boolean(getFieldValue('unicode_emoji_enabled'));
        const imageEnabled = Boolean(getFieldValue('image_meme_enabled'));
        const unicodeWeight = Number(getFieldValue('unicode_emoji_weight_bps') || 0);
        const imageWeight = Number(getFieldValue('image_meme_weight_bps') || 0);
        if (!unicodeEnabled && !imageEnabled) return Promise.reject(new Error('至少启用一种评论兜底'));
        if ((!unicodeEnabled && unicodeWeight) || (!imageEnabled && imageWeight)) {
          return Promise.reject(new Error('未启用的兜底类型权重必须为 0'));
        }
        if (unicodeWeight + imageWeight !== 10000) return Promise.reject(new Error('两类兜底权重合计必须为 10000 bps'));
        return Promise.resolve();
      },
    }),
  ];
}
