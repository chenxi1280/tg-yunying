import React from 'react';
import { Alert, Checkbox, Collapse, Descriptions, Form, Input, InputNumber, Select, Space, Typography } from 'antd';
import type { Account, AccountPool, ChannelMessage, ChannelMessageComment, OperationTarget, PromptTemplate, RuleSet, TaskCenterTaskType, TaskPrecheck } from '../types';
import { ChannelCommentTypeConfig, ChannelLikeTypeConfig, ChannelViewTypeConfig } from './TaskCenterChannelConfigSections';
import { GROUP_AI_HARD_HOURLY_MIN_MESSAGES, TASK_TYPES, TYPE_LABEL, OPERATION_PROFILE_TEMPLATES, type OperationProfileTemplateId, accountPrecheck, curveNumbers, curveText, currentOperationProfile, formatDateTime, formatPrecheckReasons, operationProfileSummary, operationTemplate, precheckReasonLabel, ruleSummary, targetName } from './taskCenterViewModel';

const targetSelectProps = {
  showSearch: true,
  optionFilterProp: "label" as const,
  filterOption: (input: string, option?: { label?: unknown }) => String(option?.label ?? "").toLowerCase().includes(input.trim().toLowerCase()),
};

export function EditBasics() {
  return (
    <>
      <Typography.Title level={5}>基础信息</Typography.Title>
      <div className="form-grid">
        <Form.Item name="name" label="任务名称" rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="scheduled_end" label="结束时间（可选）"><Input type="datetime-local" placeholder="不填则持续运行" /></Form.Item>
      </div>
      <Form.Item name="priority" hidden><InputNumber /></Form.Item>
      <Form.Item name="timezone" hidden><Input /></Form.Item>
      <Form.Item name="scheduled_start" hidden><Input /></Form.Item>
    </>
  );
}

export function WizardBasics({ taskType, onTypeChange }: { taskType: TaskCenterTaskType; onTypeChange: (type: TaskCenterTaskType) => void }) {
  return (
    <div className="form-grid">
      <Form.Item label="任务类型">
        <Select options={TASK_TYPES} value={taskType} onChange={onTypeChange} />
      </Form.Item>
      <Form.Item name="name" label="任务名称" rules={[{ required: true }]}><Input /></Form.Item>
      {taskType === 'group_membership_admission' && <Form.Item name="scheduled_start" label="开始时间" rules={[{ required: true }]}><Input type="datetime-local" /></Form.Item>}
      <Form.Item name="scheduled_end" label="结束时间（可选）"><Input type="datetime-local" placeholder="不填则持续运行" /></Form.Item>
    </div>
  );
}

export function WizardTarget({ taskType, groupTargets, channelTargets, messages, messageScope, targetChannelId, onTargetChannelChange, allowInlineTarget = true }: { taskType: TaskCenterTaskType; groupTargets: OperationTarget[]; channelTargets: OperationTarget[]; messages: ChannelMessage[]; messageScope: string; targetChannelId?: number; onTargetChannelChange: () => void; allowInlineTarget?: boolean }) {
  const groupTargetOptions = groupTargets
    .map((target) => ({
      value: target.id,
      label: `${target.title} / ${target.auth_status || '未确认'} / 可发账号 ${target.available_send_account_count} / 监听账号 ${target.listener_account_count}`,
    }));
  const sendableGroupTargetOptions = groupTargetOptions.filter((option) => groupTargets.find((target) => target.id === option.value)?.can_send);
  const channelTargetOptions = channelTargets.map((target) => ({ value: target.id, label: target.title }));
  if (taskType === 'group_ai_chat' || taskType === 'group_membership_admission') {
    return (
      <div className="form-grid">
        <Form.Item name="target_operation_target_id" label="已有运营目标群" rules={[{ required: taskType === 'group_membership_admission' }]}><Select allowClear options={groupTargetOptions} {...targetSelectProps} /></Form.Item>
        {taskType === 'group_ai_chat' && allowInlineTarget && <Form.Item name="target_input" label="粘贴新群入口"><Input placeholder="@group_name / https://t.me/+invite / peer id" /></Form.Item>}
        {taskType === 'group_ai_chat' && allowInlineTarget && <Form.Item name="target_title" label="目标名称"><Input placeholder="可选，不填时使用入口作为名称" /></Form.Item>}
      </div>
    );
  }
  if (taskType === 'group_relay') {
    return (
      <div className="form-grid">
        <Form.Item name="source_operation_target_ids" label="源群运营目标"><Select mode="multiple" allowClear options={groupTargetOptions} {...targetSelectProps} /></Form.Item>
        {allowInlineTarget && <Form.Item name="source_target_input" label="粘贴新源群入口"><Input placeholder="@source_group / 邀请链接" /></Form.Item>}
        <Form.Item name="target_operation_target_id" label="默认目标群"><Select allowClear options={groupTargetOptions} {...targetSelectProps} /></Form.Item>
        {allowInlineTarget && <Form.Item name="target_input" label="粘贴新目标群入口"><Input placeholder="@target_group / 邀请链接" /></Form.Item>}
        <Form.Item name="target_operation_target_ids" label="附加目标群"><Select mode="multiple" allowClear options={sendableGroupTargetOptions} {...targetSelectProps} /></Form.Item>
      </div>
    );
  }
  const scopedMessages = messages.filter((message) => !targetChannelId || message.channel_target_id === targetChannelId);
  return (
    <div className="form-grid">
      <Form.Item name="target_channel_id" label="已有目标频道"><Select allowClear options={channelTargetOptions} onChange={onTargetChannelChange} {...targetSelectProps} /></Form.Item>
      {allowInlineTarget && <Form.Item name="target_input" label="粘贴新频道入口"><Input placeholder="@channel / https://t.me/channel / https://t.me/+invite" /></Form.Item>}
      {allowInlineTarget && <Form.Item name="target_title" label="频道名称"><Input placeholder="可选，不填时使用入口作为名称" /></Form.Item>}
      <Form.Item name="message_scope" label="消息范围"><Select options={[{ value: 'dynamic_new', label: '持续监听新消息' }, { value: 'latest_n', label: '最新 N 条' }, { value: 'all', label: '所有消息' }, { value: 'date_range', label: '日期范围' }, { value: 'specific', label: '指定消息' }]} /></Form.Item>
      {['latest_n', 'dynamic_new'].includes(messageScope) && <Form.Item name="message_count" label={messageScope === 'dynamic_new' ? '每轮采集上限' : '消息数量'} rules={[{ required: true }]}><InputNumber min={1} max={500} /></Form.Item>}
      {messageScope === 'specific' && <Form.Item name="message_ids" label="频道消息" rules={[{ required: true }]}><Select mode="multiple" options={scopedMessages.map((message) => ({ value: message.id, label: `#${message.message_id} / ${message.content_preview || message.message_url || message.id}` }))} {...targetSelectProps} /></Form.Item>}
      {messageScope === 'date_range' && <><Form.Item name="date_from" label="开始时间"><Input type="datetime-local" /></Form.Item><Form.Item name="date_to" label="结束时间"><Input type="datetime-local" /></Form.Item></>}
    </div>
  );
}

export function WizardTypeConfig({
  taskType,
  ruleSets = [],
  slangTemplates = [],
  relaySourceOptions = [],
}: {
  taskType: TaskCenterTaskType;
  ruleSets?: RuleSet[];
  slangTemplates?: PromptTemplate[];
  comments?: ChannelMessageComment[];
  relaySourceOptions?: Array<{ value: string; label: string }>;
  targetChannelId?: number;
  messageScope?: string;
  messageIds?: Array<number | string> | string | null;
}) {
  const form = Form.useFormInstance();
  function markMessagesPerRoundManual(value: number | null) {
    if (value != null) form.setFieldValue('messages_per_round_mode', 'manual');
  }
  const replyMinPerRoundRules = [
    ({ getFieldValue }: any) => ({
      validator(_: unknown, value: number | null) {
        const total = Number(getFieldValue('messages_per_round') || 0);
        if (value != null && !Number.isInteger(Number(value))) return Promise.reject(new Error('必须填写整数'));
        if (value == null || Number(value) <= total) return Promise.resolve();
        return Promise.reject(new Error('不能大于每轮总发言数'));
      },
    }),
  ];
  const replyMinPerMessageRules = [
    ({ getFieldValue }: any) => ({
      validator(_: unknown, value: number | null) {
        const total = Number(getFieldValue('target_comments_per_message') || 0);
        if (value != null && !Number.isInteger(Number(value))) return Promise.reject(new Error('必须填写整数'));
        if (value == null || Number(value) <= total) return Promise.resolve();
        return Promise.reject(new Error('不能大于预计每条评论/回复'));
      },
    }),
  ];
  const hardHourlyRules = [
    ({ getFieldValue }: any) => ({
      validator(_: unknown, value: number | null) {
        if (!getFieldValue('hard_hourly_target_enabled')) return Promise.resolve();
        if (Number.isInteger(Number(value)) && Number(value) >= GROUP_AI_HARD_HOURLY_MIN_MESSAGES) return Promise.resolve();
        return Promise.reject(new Error(`开启后必须填写不小于 ${GROUP_AI_HARD_HOURLY_MIN_MESSAGES} 的整数`));
      },
    }),
  ];
  const accountCoverageMaxRules = [
    ({ getFieldValue }: any) => ({
      validator(_: unknown, value: number | null) {
        const minValue = Number(getFieldValue('per_account_daily_min_messages') || 0);
        if (value == null || !Number.isInteger(Number(value))) return Promise.reject(new Error('必须填写整数'));
        if (Number(value) >= minValue) return Promise.resolve();
        return Promise.reject(new Error('不能小于每账号最少消息数'));
      },
    }),
  ];

  const versionOptions = ruleSets.flatMap((ruleSet) => ruleSet.versions.filter((version) => version.status === 'published').map((version) => ({
    value: version.id,
    label: `${ruleSet.name} / v${version.version} / ${version.status === 'published' ? '已发布' : '历史版本'}`,
  })));
  const ruleFields = (
    <div className="form-grid">
      <Form.Item name="rule_set_id" label="规则集">
        <Select allowClear options={ruleSets.map((ruleSet) => ({ value: ruleSet.id, label: ruleSet.name }))} />
      </Form.Item>
      <Form.Item name="rule_set_version_id" label="规则版本">
        <Select allowClear options={versionOptions} />
      </Form.Item>
    </div>
  );
  const slangOptions = slangTemplates.map((template) => ({
    value: template.id,
    label: `${template.name} / v${template.version}`,
  }));
  if (taskType === 'group_ai_chat') {
    return (
      <Space direction="vertical" style={{ width: '100%' }}>
        <Alert type="info" showIcon message="AI 回复会按绑定规则集先过滤输入上下文，再逐条校验候选回复。" />
        <Alert type="info" showIcon message="小时上限控制总量；当天未参与账号会优先补齐，并在预算内抬高本轮下限。" />
        {ruleFields}
        <div className="form-grid">
          <Form.Item name="topic_hint" label="话题方向（可选）"><Input.TextArea rows={2} placeholder="不填时系统会按群目标方向或自然开场自动起聊" /></Form.Item>
          <Form.Item name="topic_directions" label="多个话题方向（每行一个）">
            <Input.TextArea rows={5} placeholder={'郑州楼凤妹子怎么样\n主任最近约新妹子了\n精品榜的妹子真好'} />
          </Form.Item>
          <Form.Item name="teacher_targets" label="聊天对象（每行一个）">
            <Input.TextArea rows={5} placeholder={'花花老师身材服务真好\n新人榜单妹子'} />
          </Form.Item>
          <Form.Item name="tone" label="语气"><Select options={[{ value: 'auto', label: '自动' }, { value: 'casual', label: '口语' }, { value: 'professional', label: '正式' }, { value: 'mixed', label: '混合' }]} /></Form.Item>
          <Form.Item name="slang_prompt_template_id" label="AI 黑话配置">
            <Select allowClear options={slangOptions} placeholder="选择系统设置里的 AI 黑话词表" />
          </Form.Item>
          <Form.Item name="messages_per_round_mode" label="每轮发言"><Select options={[{ value: 'auto', label: '系统自动判定' }, { value: 'manual', label: '手动指定' }]} /></Form.Item>
          <Form.Item name="messages_per_round" label="每轮总发言数"><InputNumber min={1} onChange={markMessagesPerRoundManual} /></Form.Item>
          <Form.Item name="reply_min_per_round" label="每轮最少引用回复数" dependencies={['messages_per_round']} rules={replyMinPerRoundRules}><InputNumber min={0} /></Form.Item>
          <Form.Item name="consecutive_message_enabled" label="同账号连发"><Select options={[{ value: true, label: '开启' }, { value: false, label: '关闭' }]} /></Form.Item>
          <Form.Item name="consecutive_message_min" label="连发最少条数"><InputNumber min={2} max={4} precision={0} /></Form.Item>
          <Form.Item name="consecutive_message_max" label="连发最多条数"><InputNumber min={2} max={4} precision={0} /></Form.Item>
          <Form.Item name="consecutive_message_probability" label="连发概率"><InputNumber min={0} max={1} step={0.05} /></Form.Item>
          <Form.Item name="account_coverage_mode" label="全账号日覆盖模式">
            <Select options={[{ value: 'natural', label: '关闭' }, { value: 'all_accounts_daily', label: '开启' }]} />
          </Form.Item>
          <Form.Item name="per_account_daily_min_messages" label="每账号最少消息数"><InputNumber min={1} max={2} precision={0} /></Form.Item>
          <Form.Item name="per_account_daily_max_messages" label="每账号最多消息数" dependencies={['per_account_daily_min_messages']} rules={accountCoverageMaxRules}>
            <InputNumber min={1} max={2} precision={0} />
          </Form.Item>
          <Form.Item name="coverage_window_hours" hidden><InputNumber /></Form.Item>
          <Form.Item noStyle shouldUpdate={(prev, next) => prev.account_coverage_mode !== next.account_coverage_mode}>
            {({ getFieldValue }) => getFieldValue('account_coverage_mode') === 'all_accounts_daily' ? (
              <div style={{ gridColumn: '1 / -1' }}>
                <Alert
                  type="info"
                  showIcon
                  message="系统会在 24 小时内优先补齐每个可发言账号的 1-2 条成功发言；仍受准入、账号容量、风控、AI 候选质量和小时硬上限约束。"
                />
              </div>
            ) : null}
          </Form.Item>
          <Form.Item name="hard_hourly_strategy" hidden><Input /></Form.Item>
          <Form.Item name="hard_hourly_target_enabled" valuePropName="checked">
            <Checkbox onChange={(event) => {
              if (event.target.checked) form.setFieldValue('hard_hourly_strategy', 'force_planning');
            }} disabled>启用每小时硬目标</Checkbox>
          </Form.Item>
          <Form.Item noStyle shouldUpdate={(prev, next) => prev.hard_hourly_target_enabled !== next.hard_hourly_target_enabled}>
            {({ getFieldValue }) => getFieldValue('hard_hourly_target_enabled') ? (
              <>
                <Form.Item name="hourly_min_messages" label="每小时最低发送量" dependencies={['hard_hourly_target_enabled']} rules={hardHourlyRules}>
                  <InputNumber min={GROUP_AI_HARD_HOURLY_MIN_MESSAGES} precision={0} />
                </Form.Item>
                <Form.Item label="未达标处理">
                  <Input readOnly value="强推规划" />
                </Form.Item>
                <div style={{ gridColumn: '1 / -1' }}>
                  <Alert
                    type="warning"
                    showIcon
                    message="系统会自动提高规划强度以追赶本小时最低发送量；真实执行仍受账号容量、目标权限、TG 限制、AI 质量和风控限制约束，未达标原因会在任务详情中展示。"
                  />
                </div>
              </>
            ) : null}
          </Form.Item>
        </div>
        <Collapse
          ghost
          items={[
            {
              key: 'advanced',
              label: '高级设置',
              children: (
                <div className="form-grid">
                  <Form.Item name="participation_rate" label="参与账号比例"><InputNumber min={0.01} max={1} step={0.01} /></Form.Item>
                  <Form.Item name="allow_account_repeat" label="允许账号重复发言"><Select options={[{ value: true, label: '允许，账号不足时轮换复用' }, { value: false, label: '不允许，同轮尽量一号一条' }]} /></Form.Item>
                  <Form.Item name="repeat_cooldown_rounds" label="重复冷却轮数"><InputNumber min={0} /></Form.Item>
                  <Form.Item name="chat_history_depth" label="上下文历史条数（不是账号数）"><InputNumber min={1} max={200} /></Form.Item>
                  <Form.Item name="account_memory_depth" label="账号记忆条数"><InputNumber min={0} max={20} /></Form.Item>
                  <Form.Item name="account_personas" label="账号角色">
                    <Input.TextArea rows={3} placeholder={'101=提问型账号\n102=补充细节账号'} />
                  </Form.Item>
                  <Form.Item name="idle_continuation_enabled" label="无人发言续聊"><Select options={[{ value: true, label: '开启' }, { value: false, label: '关闭' }]} /></Form.Item>
                  <Form.Item name="idle_continuation_seconds" label="续聊间隔秒数"><InputNumber min={30} max={86400} /></Form.Item>
                  <Form.Item name="context_expire_after_messages" label="上下文过期消息数"><InputNumber min={0} max={500} /></Form.Item>
                  <Form.Item name="system_prompt_override" label="System Prompt 覆盖">
                    <Input.TextArea rows={3} placeholder="为空则使用系统默认提示词" />
                  </Form.Item>
                </div>
              ),
            },
          ]}
        />
        <Collapse
          ghost
          items={[
            {
              key: 'membership-strategy',
              label: '准入策略',
              children: (
                <div className="form-grid">
                  <Form.Item name="auto_join_target" label="自动入群"><Select options={[{ value: true, label: '开启' }, { value: false, label: '关闭' }]} /></Form.Item>
                  <Form.Item name="auto_follow_required_channel" label="自动关注关联频道"><Select options={[{ value: true, label: '开启' }, { value: false, label: '关闭' }]} /></Form.Item>
                  <Form.Item name="auto_resolve_verification" label="自动处理验证"><Select options={[{ value: true, label: '开启' }, { value: false, label: '关闭' }]} /></Form.Item>
                  <Form.Item name="ai_assisted_verification" label="AI 辅助验证"><Select options={[{ value: true, label: '开启' }, { value: false, label: '关闭' }]} /></Form.Item>
                  <Form.Item name="captcha_failure_policy" label="图形验证码失败处理"><Select options={[{ value: 'manual', label: '转人工处理' }]} /></Form.Item>
                  <Form.Item name="membership_max_concurrent" label="准入子任务并发数"><InputNumber min={1} max={50} /></Form.Item>
                </div>
              ),
            },
          ]}
        />
      </Space>
    );
  }
  if (taskType === 'group_membership_admission') {
    return (
      <Space direction="vertical" style={{ width: '100%' }}>
        <Alert type="info" showIcon message="群聊准入任务会在开始时间锁定所选账号分组，逐个完成入群、验证和真实测试发言。" />
        <div className="form-grid">
          <Form.Item name="admission_max_concurrent" label="准入最大并发" rules={[{ required: true }]}><InputNumber min={1} max={50} /></Form.Item>
          <Form.Item name="admission_per_minute" label="每分钟处理数" rules={[{ required: true }]}><InputNumber min={1} max={200} /></Form.Item>
          <Form.Item name="test_message_min_chars" label="测试发言最少字数" rules={[{ required: true }]}><InputNumber min={1} max={80} /></Form.Item>
          <Form.Item name="test_message_max_chars" label="测试发言最多字数" rules={[{ required: true }]}><InputNumber min={1} max={120} /></Form.Item>
          <Form.Item name="delete_after_send" valuePropName="checked"><Checkbox>发送成功后尝试删除测试消息</Checkbox></Form.Item>
        </div>
      </Space>
    );
  }
  if (taskType === 'group_relay') {
    return (
      <Space direction="vertical" style={{ width: '100%' }}>
        <Alert
          type="info"
          showIcon
          message={ruleSets.length ? '已加载默认运营规则集，可直接绑定任务使用。' : '正在初始化默认运营规则集。'}
        />
        <div className="form-grid">
          <div style={{ gridColumn: '1 / -1' }}>{ruleFields}</div>
          <Form.Item name="content_mode" label="转发处理方式">
            <Select options={[{ value: 'raw', label: '原文' }, { value: 'light_rewrite', label: '轻量改写' }, { value: 'ai_rewrite', label: 'AI 改写' }, { value: 'summary', label: '摘要' }]} />
          </Form.Item>
        </div>
        <Collapse
          ghost
          items={[
            {
              key: 'source-filter',
              label: '来源过滤配置',
              children: (
                <Space direction="vertical" style={{ width: '100%' }}>
                  <div className="form-grid">
                    <Form.Item name="filter_bot_messages" valuePropName="checked">
                      <Checkbox>屏蔽机器人消息</Checkbox>
                    </Form.Item>
                    <Form.Item name="filter_admin_messages" valuePropName="checked">
                      <Checkbox>不转发群主和管理员消息</Checkbox>
                    </Form.Item>
                    <Form.Item name="excluded_sender_peer_ids" label="从最近来源发言人中选择">
                      <Select mode="multiple" allowClear options={relaySourceOptions} placeholder="任务运行后可从最近来源发言人中选择" />
                    </Form.Item>
                    <Form.Item name="excluded_sender_input" label="手动粘贴 @username / sender_peer_id / 昵称">
                      <Input.TextArea rows={3} placeholder={'一行一个\n@username\nid:123456789\n昵称'} />
                    </Form.Item>
                  </div>
                  <Typography.Text type="secondary">优先保存 sender_peer_id，其次 @username；昵称只作为兜底，可能误伤同名成员。</Typography.Text>
                </Space>
              ),
            },
          ]}
        />
      </Space>
    );
  }
  if (taskType === 'channel_view') {
    return <ChannelViewTypeConfig />;
  }
  if (taskType === 'channel_like') {
    return <ChannelLikeTypeConfig />;
  }
  return <ChannelCommentTypeConfig replyMinPerMessageRules={replyMinPerMessageRules} ruleFields={ruleFields} />;
}

export function WizardOperationProfile({ form, values, taskType }: { form: any; values: Record<string, any>; taskType: TaskCenterTaskType }) {
  const selectedTemplate = operationTemplate(values.operation_template_id);
  const curve = curveNumbers(values.hourly_activity_curve ?? selectedTemplate.curve);
  const isAiGroup = taskType === 'group_ai_chat';
  const currentHour = new Date().getHours();
  const currentRounds = curve[currentHour] ?? 0;
  const messagesPerRound = Number(values.messages_per_round || 0);
  const hourlyLimit = Number(values.max_actions_per_hour || 0);
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Alert
        type="info"
        showIcon
        message={
          isAiGroup
            ? `AI 活跃群曲线表示每小时启动轮数：当前 ${String(currentHour).padStart(2, '0')}:00 为 ${currentRounds} 轮/小时；每轮上限和小时硬上限共同决定发送量。`
            : `预计运行摘要：${operationProfileSummary(values)}。频道类任务会按曲线、账号容量和风控分配动作预算。`
        }
      />
      {isAiGroup && (
        <Alert
          type="info"
          showIcon
          message={`本小时理论最大发送：${hourlyLimit && messagesPerRound ? Math.min(currentRounds * messagesPerRound, hourlyLimit) : currentRounds * Math.max(1, messagesPerRound || 1)} 条；曲线不再压低参与账号比例。`}
        />
      )}
      <div className="form-grid">
        <Form.Item name="operation_template_id" label="24 小时活跃曲线">
          <Select
            options={OPERATION_PROFILE_TEMPLATES.map((item) => ({ value: item.value, label: item.label }))}
            onChange={(value: OperationProfileTemplateId) => {
              const template = operationTemplate(value);
              form.setFieldsValue({
                operation_template_id: template.value,
                hourly_activity_curve: curveText(template.curve),
                operation_profile_manual_override: false,
              });
            }}
          />
        </Form.Item>
        <Form.Item label="曲线摘要">
          <Input readOnly value={curve.map((value, hour) => `${String(hour).padStart(2, '0')}:${value}`).join('  ')} />
        </Form.Item>
      </div>
      <Collapse
        ghost
        items={[
          {
            key: 'curve',
            label: '手动微调曲线',
            children: (
              <div className="form-grid">
                <Form.Item name="hourly_activity_curve" label={isAiGroup ? '每小时轮数' : '小时预算权重'}>
                  <Input.TextArea
                    rows={3}
                    onChange={() => form.setFieldsValue({ operation_profile_manual_override: true })}
                  />
                </Form.Item>
                <Form.Item name="quiet_threshold" label="低频阈值"><InputNumber min={0} max={60} /></Form.Item>
                <Form.Item name="peak_threshold" label="高峰阈值"><InputNumber min={0} max={60} /></Form.Item>
                <Form.Item name="operation_profile_manual_override" hidden><Checkbox /></Form.Item>
              </div>
            ),
          },
        ]}
      />
    </Space>
  );
}

export function TaskRuntimeAdvancedFields() {
  return (
    <>
      <Form.Item name="max_concurrent" label="账号并发上限（账号数）"><InputNumber min={1} max={500} /></Form.Item>
      <Form.Item name="cooldown_per_account_minutes" label="账号冷却分钟"><InputNumber min={0} /></Form.Item>
      <Form.Item name="ban_policy" label="异常账号处理"><Select options={[{ value: 'skip', label: '跳过账号' }, { value: 'pause_task', label: '暂停任务' }, { value: 'alert', label: '只告警' }]} /></Form.Item>
      <Form.Item name="max_actions_per_hour" label="每小时最大发送量"><InputNumber min={1} placeholder="预检后按账号数推荐" /></Form.Item>
      <Form.Item name="max_retries" label="失败重试次数"><InputNumber min={0} max={10} /></Form.Item>
    </>
  );
}

export function WizardAccounts({ accountMode, accounts, accountPools, taskType }: { accountMode: string; accounts: Account[]; accountPools: AccountPool[]; taskType: TaskCenterTaskType }) {
  if (taskType === 'group_membership_admission') {
    return (
      <div className="form-grid">
        <Form.Item name="account_group_ids" label="账号分组" rules={[{ required: true }]}><Select mode="multiple" options={accountPools.map((pool) => ({ value: pool.id, label: `${pool.name} (${pool.account_count})` }))} /></Form.Item>
      </div>
    );
  }
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <div className="form-grid">
        <Form.Item name="selection_mode" label="账号选择"><Select options={[{ value: 'all', label: '全部账号' }, { value: 'group', label: '账号分组' }, { value: 'manual', label: '手动选择' }]} /></Form.Item>
        {accountMode === 'group' && <Form.Item name="account_group_id" label="账号分组" rules={[{ required: true }]}><Select options={accountPools.map((pool) => ({ value: pool.id, label: `${pool.name} (${pool.account_count})` }))} /></Form.Item>}
        {accountMode === 'manual' && <Form.Item name="account_ids" label="账号" rules={[{ required: true }]}><Select mode="multiple" options={accounts.map((account) => ({ value: account.id, label: `${account.display_name} / ${account.status}` }))} /></Form.Item>}
      </div>
    </Space>
  );
}

export function WizardReview({ taskType, values, accounts, accountPools, targets, ruleSets, slangTemplates, precheck, loading }: { taskType: TaskCenterTaskType; values: Record<string, any>; accounts: Account[]; accountPools: AccountPool[]; targets: OperationTarget[]; ruleSets: RuleSet[]; slangTemplates: PromptTemplate[]; precheck: TaskPrecheck | null; loading: boolean }) {
  const account = accountPrecheck(values, accounts, accountPools);
  const profile = currentOperationProfile(values);
  const selectedSlang = slangTemplates.find((template) => template.id === values.slang_prompt_template_id);
  const targetSummary = taskType === 'group_relay'
    ? values.target_operation_target_ids?.length
      ? `运营目标 #${values.target_operation_target_id || '-'} + ${values.target_operation_target_ids.length} 个附加目标`
      : `运营目标 #${values.target_operation_target_id || '-'}`
    : values.target_operation_target_id
      ? `运营目标 #${values.target_operation_target_id}`
      : values.target_channel_id
        ? `频道 #${values.target_channel_id}`
        : '-';
  const displayTarget = targetName(values, targets);
  const precheckStatus = loading ? '预检中' : precheck ? precheck.decision === 'allow' ? '通过' : precheck.decision === 'warn' ? '有风险' : '阻塞' : '未执行';
  const recommended = precheck?.capacity_summary?.recommended_limits;
  const replyReference = precheck?.capacity_summary?.reply_reference_summary;
  const profileUnit = taskType === 'group_ai_chat' ? '轮/小时' : '权重';
  const recommendedSummary = recommended ? [
    recommended.current_hour_rounds ? `当前轮数 ${recommended.current_hour_rounds}` : '',
    recommended.max_actions_per_hour ? `每小时 ${recommended.max_actions_per_hour}` : '',
    recommended.messages_per_round ? `每轮 ${recommended.messages_per_round}` : '',
    recommended.estimated_hourly_capacity ? `理论小时容量 ${recommended.estimated_hourly_capacity}` : '',
    recommended.target_comments_per_message ? `每条 ${recommended.target_comments_per_message}` : '',
    recommended.max_comments_per_account_per_hour ? `每号每小时 ${recommended.max_comments_per_account_per_hour}` : '',
  ].filter(Boolean).join('；') : '等待预检';
  const resolution = precheck?.target_resolution;
  const replySummary = taskType === 'group_ai_chat'
    ? `每轮总发言 ${values.messages_per_round || 1}，最少引用回复 ${values.reply_min_per_round || 0}`
    : taskType === 'channel_comment'
      ? `每条目标 ${values.target_comments_per_message || 1}，最少引用回复 ${values.reply_min_per_message || 0}`
      : '-';
  const replyReferenceText = replyReference
    ? `；可引用 ${replyReference.available_reference_count ?? 0}，缺口 ${replyReference.shortfall_count ?? 0}`
    : '';
  const hardTarget = precheck?.hard_hourly_target;
  const hardTargetSummary = values.hard_hourly_target_enabled
    ? [
      `目标 ${values.hourly_min_messages || hardTarget?.hourly_min_messages || '-'} 条/小时`,
      hardTarget?.estimated_hourly_capacity != null ? `估算容量 ${hardTarget.estimated_hourly_capacity}` : '',
      hardTarget?.capacity_gap != null ? `容量缺口 ${hardTarget.capacity_gap}` : '',
      hardTarget?.warnings?.length ? hardTarget.warnings.join('；') : '',
    ].filter(Boolean).join('；')
    : '未启用';
  const resolutionItems = [...(resolution?.sources || []), ...(resolution?.targets || [])];
  const resolutionSummary = resolutionItems.length
    ? resolutionItems.map((item: any) => `${item.role === 'listen_source' ? '源' : '目标'} ${item.status || 'resolved'} / #${item.target_id || '-'} / ${item.title || item.tg_peer_id || item.target_input || '-'}`).join('；')
    : resolution?.target_id
      ? `${resolution.status || 'resolved'} / #${resolution.target_id} / ${resolution.title || resolution.tg_peer_id || '-'}`
      : values.target_input || values.source_target_input || '使用已有目标';
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      {precheck && (
        <Alert
          type={precheck.decision === 'block' ? 'error' : precheck.decision === 'warn' ? 'warning' : 'success'}
          showIcon
          message={`创建前预检：${precheckStatus}`}
          description={formatPrecheckReasons([...precheck.blockers, ...precheck.warnings, ...precheck.risk_hits]) || '账号、目标、规则和风控检查通过'}
        />
      )}
      <Descriptions bordered column={2} size="small" items={[
      { key: 'type', label: '任务类型', children: TYPE_LABEL[taskType] },
      { key: 'name', label: '任务名称', children: values.name || '-' },
      { key: 'end', label: '结束时间', children: values.scheduled_end ? formatDateTime(values.scheduled_end) : '不限制' },
      { key: 'target', label: '任务目标', children: displayTarget === '-' ? targetSummary : displayTarget },
      { key: 'targetResolution', label: '目标解析', children: resolutionSummary },
      { key: 'account', label: '账号摘要', children: precheck ? `候选 ${precheck.candidate_account_count} 个，可用 ${precheck.available_account_count} 个，受限 ${precheck.limited_account_count} 个，阻塞 ${precheck.blocked_account_count} 个` : `${account.label}，候选 ${account.total} 个，当前在线 ${account.online} 个，受限/离线 ${account.limited} 个` },
      { key: 'membership', label: '准入前置', children: precheck?.membership_subtask_preview?.subtask_type ? `已满足 ${precheck.ready_account_count} 个，待准备 ${precheck.preparable_account_count} 个，预计准入动作 ${precheck.estimated_membership_actions} 个，进度 ${precheck.membership_subtask_preview.progress_percent ?? 0}%` : '无额外准入动作' },
      { key: 'targetAbility', label: '目标能力', children: precheck?.target_ability?.length ? precheck.target_ability.map((item) => `${item.title || item.target_id} / ${item.can_task ? '可创建任务' : item.auth_status || '不可用'}`).join('；') : displayTarget },
      { key: 'estimate', label: '预计动作量', children: precheck ? `预计 ${precheck.estimated_actions} 条，容量缺口 ${precheck.capacity_shortfall}` : '等待预检' },
      { key: 'reply', label: '引用回复配置', children: `${replySummary}${replyReferenceText}` },
      { key: 'hard-hourly', label: '每小时硬目标', children: hardTargetSummary },
      { key: 'recommend', label: '推荐数量', children: precheck?.round_capacity_explanation ?? recommendedSummary },
      { key: 'capacity', label: '容量口径', children: precheck?.capacity_summary ? `目标每条 ${precheck.capacity_summary.target_per_message ?? 0}，有效账号 ${precheck.capacity_summary.effective_account_count ?? 0}，最大并发 ${precheck.capacity_summary.max_concurrent ?? 0}，缺口 ${precheck.capacity_summary.capacity_shortfall ?? 0}。${precheck.capacity_summary.limit_note ?? ''}` : '等待预检' },
      { key: 'pacing', label: '曲线摘要', children: `${operationProfileSummary(values)}；当前 ${String(profile.hour).padStart(2, '0')}:00 ${profile.intensity} ${profileUnit}，${profile.mode}运行` },
      { key: 'rule', label: '规则版本', children: precheck?.rule_version ? `规则集 #${precheck.rule_version.rule_set_id} / v${precheck.rule_version.version} / ${precheck.rule_version.status}` : ['group_relay', 'group_ai_chat', 'channel_comment'].includes(taskType) ? ruleSummary(values, ruleSets) : '平台默认规则' },
      { key: 'ai', label: 'AI 摘要', children: taskType === 'group_ai_chat' ? `语气 ${values.tone || 'auto'}，黑话集 ${selectedSlang ? `${selectedSlang.name} / v${selectedSlang.version}` : '系统默认语气'}` : taskType === 'channel_comment' ? `评论方向 ${values.comment_style || 'mixed'}，主题 ${values.topic_hint || '按消息内容'}` : '-' },
      { key: 'risk', label: '风控命中', children: precheck?.risk_hits?.length ? precheck.risk_hits.map(precheckReasonLabel).join('；') : `每小时最大发送量 ${values.max_actions_per_hour || '按系统默认'}，失败重试 ${values.max_retries ?? 3} 次` },
      { key: 'blockers', label: '阻塞项', children: precheck?.blockers?.length ? precheck.blockers.map(precheckReasonLabel).join('；') : '无' },
      { key: 'mode', label: '启动说明', children: precheck?.decision === 'block' ? '当前预检存在阻塞项，需处理后再启动。' : account.online > 0 ? '创建后 worker 会再次校验账号、目标、规则和风控，再按曲线执行。' : '当前账号范围没有在线账号，创建后会等待账号恢复。' },
    ]} />
    </Space>
  );
}
