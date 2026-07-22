import React from 'react';
import { Alert, Checkbox, Collapse, Descriptions, Form, Input, InputNumber, Select, Space, Typography } from 'antd';
import type { Account, AccountPool, ChannelMessageComment, OperationTarget, PromptTemplate, RuleSet, TaskCenterTaskType, TaskPrecheck } from '../types';
import { ChannelCommentTypeConfig, ChannelLikeTypeConfig, ChannelViewTypeConfig } from './TaskCenterChannelConfigSections';
import { GROUP_AI_HARD_HOURLY_MIN_MESSAGES, TASK_TYPES, TYPE_LABEL, OPERATION_PROFILE_TEMPLATES, type OperationProfileTemplateId, accountPrecheck, curveNumbers, curveText, currentOperationProfile, formatDateTime, formatPrecheckReasons, operationProfileSummary, operationTemplate, precheckReasonLabel, ruleSummary, targetName, words } from './taskCenterViewModel';

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
  const simpleSearchClickTask = taskType === 'search_join_group' || taskType === 'search_rank_deboost';
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <div className="form-grid">
        <Form.Item label="任务类型">
          <Select options={TASK_TYPES} value={taskType} onChange={onTypeChange} />
        </Form.Item>
        {!simpleSearchClickTask && <Form.Item name="name" label="任务名称" rules={[{ required: true }]}><Input /></Form.Item>}
        {taskType === 'group_membership_admission' && <Form.Item name="scheduled_start" label="开始时间" rules={[{ required: true }]}><Input type="datetime-local" /></Form.Item>}
        {!simpleSearchClickTask && <Form.Item name="scheduled_end" label="结束时间（可选）"><Input type="datetime-local" placeholder="不填则持续运行" /></Form.Item>}
      </div>
      {simpleSearchClickTask && <Alert type="info" showIcon message="任务名称、代理、机器人与风控策略由系统管理；下一步可配置账号组与执行节奏。" />}
    </Space>
  );
}

function SimpleSearchClickConfig({
  taskType,
  editing = false,
  allowUncappedTargetCount = false,
}: {
  taskType: TaskCenterTaskType;
  editing?: boolean;
  allowUncappedTargetCount?: boolean;
}) {
  const isRankDeboost = taskType === 'search_rank_deboost';
  const targetField = 'target_count';
  const targetLabel = '目标次数';
  const keywordRequired = !editing || isRankDeboost;
  const normalTargetRules = editing || allowUncappedTargetCount
    ? []
    : [{ required: true, message: '请填写每日目标次数' }];
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Alert
        type="info"
        showIcon
        message={isRankDeboost ? '系统负责账号资格、代理、机器人和风险闸门；启动时仍会检查黑账号组的真实执行条件。' : '系统负责账号资格、代理、机器人和风险闸门；账号组与执行节奏在下一步配置。'}
        description={isRankDeboost
          ? '目标次数只统计已确认的目标点击；待执行或结果未知的动作会占用额度，避免重复点击。'
          : '目标点击与成员关系分开统计：命中目标即计点击，只有观察到 membership_observed 才计加入。当天点击达标后任务继续运行，次日按新自然日重新计算。'}
      />
      <div className="form-grid">
        <Form.Item name="keywords" label={editing && !isRankDeboost ? '搜索关键词（留空不变）' : '搜索关键词'} rules={keywordRequired ? [{ required: true, message: '请填写至少一个搜索关键词' }] : []}>
          <Input.TextArea rows={4} placeholder={'上海 留学\n上海 国际学校'} />
        </Form.Item>
        {isRankDeboost ? (
          <Form.Item name={targetField} label={allowUncappedTargetCount ? `${targetLabel}（留空保持历史不封顶）` : targetLabel} rules={allowUncappedTargetCount ? [] : [{ required: true, message: `请填写${targetLabel}` }]}>
            <InputNumber min={1} precision={0} style={{ width: '100%' }} />
          </Form.Item>
        ) : (
          <>
            <Form.Item name="daily_click_target_count" label={editing ? '每日目标点击次数（留空保持原口径）' : '每日目标点击次数'} rules={normalTargetRules}>
              <InputNumber min={1} precision={0} style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item name="daily_target_count" label={editing ? '每日成员关系观察目标（留空不变）' : '每日成员关系观察目标'} rules={normalTargetRules}>
              <InputNumber min={1} precision={0} style={{ width: '100%' }} />
            </Form.Item>
          </>
        )}
      </div>
    </Space>
  );
}

function quietHoursRules(peerField: 'quiet_start' | 'quiet_end') {
  return [
    ({ getFieldValue }: any) => ({
      validator(_: unknown, value?: string) {
        const peer = String(getFieldValue(peerField) || '').trim();
        const current = String(value || '').trim();
        if (!current && !peer) return Promise.resolve();
        if (!current || !peer) return Promise.reject(new Error('请同时填写静默开始和结束时间'));
        if (current === peer) return Promise.reject(new Error('静默开始和结束时间不能相同'));
        return Promise.resolve();
      },
    }),
  ];
}

function StrictDailyTargetOptIn({ enabled, visible }: { enabled: boolean; visible: boolean }) {
  if (!visible) return null;
  if (enabled) {
    return (
      <Form.Item label="每日目标执行">
        <Typography.Text type="success">已启用严格每日目标，不使用随机跳过。</Typography.Text>
      </Form.Item>
    );
  }
  return (
    <Form.Item
      name="enable_strict_daily_target"
      label="每日目标执行"
      valuePropName="checked"
      extra="启用后会取消隐藏的随机跳过；代理、账号资格、风控和第三方检索失败仍按真实结果记录。"
    >
      <Checkbox>严格每日目标（不使用随机跳过）</Checkbox>
    </Form.Item>
  );
}

export function SearchClickExecutionConfig({
  taskType,
  accountPools,
  editing = false,
  strictDailyTargetEnabled = false,
  showStrictDailyTargetOptIn = false,
}: {
  taskType: TaskCenterTaskType;
  accountPools: AccountPool[];
  editing?: boolean;
  strictDailyTargetEnabled?: boolean;
  showStrictDailyTargetOptIn?: boolean;
}) {
  const isRankDeboost = taskType === 'search_rank_deboost';
  const requiredPurpose = isRankDeboost ? 'rank_deboost' : 'normal';
  const poolOptions = accountPools
    .filter((pool) => pool.pool_purpose === requiredPurpose && pool.is_enabled)
    .map((pool) => ({ value: pool.id, label: `${pool.name}（${pool.account_count} 个账号）` }));
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Alert
        type="info"
        showIcon
        message={isRankDeboost ? '仅可选择启用的黑搜索账号组。日抖动会在任务时区当日可执行时段分散计划，每小时抖动只在选中小时内延后；代理绑定、账号健康和真实结果仍由系统校验。' : '仅可选择启用的普通账号组。日抖动会在任务时区当日可执行时段分散计划，每小时抖动只在选中小时内延后；代理、账号健康和真实结果仍由系统校验。'}
      />
      <div className="form-grid">
        <Form.Item name="account_group_id" label="执行账号组" rules={[{ required: true, message: '请选择执行账号组' }]}>
          <Select options={poolOptions} placeholder={isRankDeboost ? '请选择黑搜索账号组' : '请选择普通账号组'} />
        </Form.Item>
        <Form.Item
          name="max_actions_per_day"
          label="每天 action 上限"
          rules={[
            { required: true, message: '请填写每天 action 上限' },
            ({ getFieldValue }: any) => ({
              validator(_: unknown, value?: number) {
                const dailyTarget = Number(getFieldValue('daily_click_target_count') || getFieldValue('daily_target_count') || 0);
                if (isRankDeboost || !dailyTarget || Number(value) >= dailyTarget) return Promise.resolve();
                return Promise.reject(new Error('每天 action 上限不能小于每日目标点击次数'));
              },
            }),
          ]}
        >
          <InputNumber min={1} precision={0} style={{ width: '100%' }} />
        </Form.Item>
        {!isRankDeboost && (
          <Form.Item
            name="per_account_daily_action_limit"
            label="单账号每日上限"
            extra="0 表示不设此项上限，仍受同关键词每日上限约束。"
            rules={[{ required: true, message: '请填写单账号每日上限' }]}
          >
            <InputNumber min={0} max={1000} precision={0} style={{ width: '100%' }} />
          </Form.Item>
        )}
        {!isRankDeboost && (
          <Form.Item
            name="allow_same_account_repeat_application"
            label="同账号重复申请"
            valuePropName="checked"
            extra="开启后，同一账号当天可为新的搜索来源再次申请入群；同一条来源仍只会创建一个准入子动作。"
          >
            <Checkbox>允许同账号当天重复申请</Checkbox>
          </Form.Item>
        )}
        {editing && !isRankDeboost && (
          <>
            <Form.Item name="actions_per_round" label="每轮计划点击数" rules={[{ required: true, message: '请填写每轮计划点击数' }]}>
              <InputNumber min={1} max={20} precision={0} style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item name="max_actions_per_hour" label="每小时最大搜索点击数" rules={[{ required: true, message: '请填写每小时最大搜索点击数' }]}>
              <InputNumber min={1} max={500} precision={0} style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item name="hourly_min_successful_joins" label="每小时最低计划点击数" rules={[{ required: true, message: '请填写每小时最低计划点击数' }]}>
              <InputNumber min={1} max={500} precision={0} style={{ width: '100%' }} />
            </Form.Item>
          </>
        )}
        {!isRankDeboost && <StrictDailyTargetOptIn enabled={strictDailyTargetEnabled} visible={showStrictDailyTargetOptIn} />}
        <Form.Item name="scheduled_end" label="完成截止时间" rules={[{ required: true, message: '请选择完成截止时间' }]}>
          <Input type="datetime-local" />
        </Form.Item>
        <Form.Item name="daily_jitter_percent" label="日抖动（%）" rules={[{ required: true, message: '请填写日抖动' }]}>
          <InputNumber min={0} max={100} precision={0} style={{ width: '100%' }} />
        </Form.Item>
        <Form.Item name="hourly_jitter_percent" label="每小时抖动（%）" rules={[{ required: true, message: '请填写每小时抖动' }]}>
          <InputNumber min={0} max={100} precision={0} style={{ width: '100%' }} />
        </Form.Item>
        <Form.Item name="quiet_start" label="静默开始（可选）" rules={quietHoursRules('quiet_end')}>
          <Input type="time" />
        </Form.Item>
        <Form.Item name="quiet_end" label="静默结束（可选）" rules={quietHoursRules('quiet_start')}>
          <Input type="time" />
        </Form.Item>
      </div>
    </Space>
  );
}

export function WizardTypeConfig({
  taskType,
  ruleSets = [],
  slangTemplates = [],
  relaySourceOptions = [],
  simpleSearchCreation = false,
  simpleSearchEditing = false,
  simpleSearchLegacyUncapped = false,
}: {
  taskType: TaskCenterTaskType;
  ruleSets?: RuleSet[];
  slangTemplates?: PromptTemplate[];
  comments?: ChannelMessageComment[];
  relaySourceOptions?: Array<{ value: string; label: string }>;
  targetChannelId?: number;
  messageScope?: string;
  messageIds?: Array<number | string> | string | null;
  simpleSearchCreation?: boolean;
  simpleSearchEditing?: boolean;
  simpleSearchLegacyUncapped?: boolean;
}) {
  const form = Form.useFormInstance();
  if (simpleSearchCreation && (taskType === 'search_join_group' || taskType === 'search_rank_deboost')) {
    return <SimpleSearchClickConfig taskType={taskType} editing={simpleSearchEditing} allowUncappedTargetCount={simpleSearchLegacyUncapped} />;
  }
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
          <Form.Item name="topic_directions" label="话题方向（每行一个）">
            <Input.TextArea rows={5} placeholder={'郑州楼凤妹子怎么样\n主任最近约新妹子了\n精品榜的妹子真好'} />
          </Form.Item>
          <Form.Item name="teacher_targets" label="讨论老师（每行一个）">
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
  if (taskType === 'search_join_group') {
    return (
      <Space direction="vertical" style={{ width: '100%' }}>
        <Alert
          type="warning"
          showIcon
          message="搜索目标群点击任务首版固定 mtproto_userbot；实时 pacing / random decision 不调用 LLM。缺少真实协议样本、代理出口或客户端元数据时 fail closed，不会假成功。已在目标群内的账号仍会执行搜索和目标确认。"
          description="membership_observed 表示完成搜索目标点击后观察到成员关系；不设固定翻页上限，只有命中目标才结束本轮成功搜索。真实末页未命中写 target_not_in_results / no_next_page 和实际页码，任务继续规划。"
        />
        <div className="form-grid">
          <Form.Item name="search_bots" label="搜索机器人" rules={[{ required: true }]}>
            <Input placeholder="jisou，可用逗号或换行配置多个" />
          </Form.Item>
          <Form.Item name="keywords" label="关键词列表" rules={[{ required: true }]}>
            <Input.TextArea rows={4} placeholder={'上海 留学\n上海 国际学校'} />
          </Form.Item>
          <Form.Item name="keyword_hashes" hidden><Input /></Form.Item>
          <Form.Item name="business_region" label="业务地区"><Input placeholder="CN-SH" /></Form.Item>
          <Form.Item name="account_locale" label="账号语言"><Input placeholder="zh-CN" /></Form.Item>
          <Form.Item name="proxy_country" label="代理出口国家"><Input placeholder="SG / JP / US" /></Form.Item>
          <Form.Item name="pre_join_decoy_click_max" label="目标确认前非目标浏览上限"><InputNumber min={0} max={3} precision={0} /></Form.Item>
          <Form.Item name="hourly_min_successful_joins" label="每小时最低成功点击"><InputNumber min={1} max={500} precision={0} /></Form.Item>
          <Form.Item name="actions_per_round" label="每轮规划数"><InputNumber min={1} max={20} precision={0} /></Form.Item>
          <Form.Item name="target_relevance_score" label="目标资料相关性"><InputNumber min={0} max={100} precision={0} /></Form.Item>
          <Form.Item name="target_content_health" label="内容健康"><Select options={[{ value: 'healthy', label: '健康' }, { value: 'weak', label: '偏弱' }, { value: 'blocked', label: '阻断' }, { value: 'unknown', label: '未知' }]} /></Form.Item>
          <Form.Item name="jisou_ecosystem_status" label="极搜生态"><Select options={[{ value: 'bot_joined', label: '已收录/机器人入驻' }, { value: 'flow_alliance', label: '流量联盟' }, { value: 'unknown', label: '未知' }]} /></Form.Item>
          <Form.Item name="paid_keyword_ad_status" label="付费关键词广告"><Select options={[{ value: 'none', label: '无' }, { value: 'active', label: '投放中' }, { value: 'expired', label: '已过期' }, { value: 'unknown', label: '未知' }]} /></Form.Item>
        </div>
        <Collapse
          ghost
          items={[
            {
              key: 'search-join-pacing',
              label: '搜索节奏与账号上限',
              children: (
                <Space direction="vertical" style={{ width: '100%' }}>
                  <Alert
                    type="info"
                    showIcon
                    message="账号每日上限、任务每日总上限填 0 表示不设上限，保存前请确认账号风险。"
                  />
                  <div className="form-grid">
                    <Form.Item name="per_account_total_action_limit" label="单账号总上限"><InputNumber min={0} max={100000} precision={0} /></Form.Item>
                    <Form.Item name="per_account_daily_action_limit" label="单账号每日上限"><InputNumber min={0} max={1000} precision={0} /></Form.Item>
                    <Form.Item name="per_account_cooldown_days" label="账号间隔天数"><InputNumber min={0} max={365} precision={0} /></Form.Item>
                    <Form.Item name="per_keyword_account_daily_limit" label="同关键词每日上限"><InputNumber min={0} max={1000} precision={0} /></Form.Item>
                    <Form.Item name="max_actions_per_day" label="任务每日总上限"><InputNumber min={0} max={100000} precision={0} /></Form.Item>
                    <Form.Item name="hourly_skip_probability" label="小时跳过概率"><InputNumber min={0} max={1} step={0.01} /></Form.Item>
                    <Form.Item name="daily_skip_probability" label="天跳过概率"><InputNumber min={0} max={1} step={0.01} /></Form.Item>
                    <Form.Item name="skip_probability_per_action" label="单次跳过概率"><InputNumber min={0} max={1} step={0.01} /></Form.Item>
                    <Form.Item name="hourly_jitter_percent" label="小时抖动百分比"><InputNumber min={0} max={100} precision={0} /></Form.Item>
                    <Form.Item name="daily_jitter_percent" label="天抖动百分比"><InputNumber min={0} max={100} precision={0} /></Form.Item>
                  </div>
                </Space>
              ),
            },
          ]}
        />
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

export function TaskRuntimeAdvancedFields({ taskType }: { taskType?: TaskCenterTaskType }) {
  const searchJoinTask = taskType === 'search_join_group';
  return (
    <>
      <Form.Item name="max_concurrent" label="账号并发上限（账号数）"><InputNumber min={1} max={500} /></Form.Item>
      <Form.Item name="cooldown_per_account_minutes" label="账号冷却分钟"><InputNumber min={0} /></Form.Item>
      <Form.Item name="ban_policy" label="异常账号处理"><Select options={[{ value: 'skip', label: '跳过账号' }, { value: 'pause_task', label: '暂停任务' }, { value: 'alert', label: '只告警' }]} /></Form.Item>
      <Form.Item name="max_actions_per_hour" label={searchJoinTask ? '每小时最大搜索点击数' : '每小时最大发送量'}><InputNumber min={searchJoinTask ? 0 : 1} placeholder="预检后按账号数推荐" /></Form.Item>
      <Form.Item name="max_retries" label="失败重试次数"><InputNumber min={0} max={10} /></Form.Item>
    </>
  );
}

function accountPoolSelectOptions(accountPools: AccountPool[], taskType: TaskCenterTaskType) {
  return accountPools.map((pool) => {
    const baseLabel = `${pool.name} (${pool.account_count})`;
    if (taskType !== 'search_rank_deboost' && pool.pool_purpose === 'rank_deboost') {
      return { value: pool.id, label: `${baseLabel} — 排名观察专用，不可参与本任务`, disabled: true };
    }
    return { value: pool.id, label: baseLabel };
  });
}

export function WizardAccounts({ accountMode, accounts, accountPools, taskType }: { accountMode: string; accounts: Account[]; accountPools: AccountPool[]; taskType: TaskCenterTaskType }) {
  if (taskType === 'group_membership_admission') {
    return (
      <div className="form-grid">
        <Form.Item name="account_group_ids" label="账号分组" rules={[{ required: true }]}><Select mode="multiple" options={accountPoolSelectOptions(accountPools, taskType)} /></Form.Item>
      </div>
    );
  }
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <div className="form-grid">
        <Form.Item name="selection_mode" label="账号选择"><Select options={[{ value: 'all', label: '全部账号' }, { value: 'group', label: '账号分组' }, { value: 'manual', label: '手动选择' }]} /></Form.Item>
        {accountMode === 'group' && <Form.Item name="account_group_id" label="账号分组" rules={[{ required: true }]}><Select options={accountPoolSelectOptions(accountPools, taskType)} /></Form.Item>}
        {accountMode === 'manual' && <Form.Item name="account_ids" label="账号" rules={[{ required: true }]}><Select mode="multiple" options={accounts.map((account) => ({ value: account.id, label: `${account.display_name} / ${account.status}` }))} /></Form.Item>}
      </div>
    </Space>
  );
}

function SimpleSearchClickReview({ taskType, values, targets, accountPools }: Pick<Parameters<typeof WizardReview>[0], 'taskType' | 'values' | 'targets' | 'accountPools'>) {
  const displayTarget = targetName(values, targets);
  const isRankDeboost = taskType === 'search_rank_deboost';
  const targetCount = values.target_count;
  const accountPool = accountPools.find((pool) => pool.id === values.account_group_id);
  const quietHours = values.quiet_start && values.quiet_end ? `${values.quiet_start} - ${values.quiet_end}` : '未设置';
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Alert
        type="info"
        showIcon
        message="系统将在可用账号、代理与风控策略范围内执行，无法确认的动作不会计入目标次数。"
      />
      <Descriptions bordered column={2} size="small" items={[
        { key: 'type', label: '任务类型', children: TYPE_LABEL[taskType] },
        { key: 'target', label: '目标群', children: displayTarget },
        { key: 'keywords', label: '搜索关键词', children: words(values.keywords).join('、') || '-' },
        ...(isRankDeboost ? [{ key: 'target-count', label: '目标次数', children: `${targetCount || '-'} 次（以已确认目标点击计）` }] : [
          { key: 'click-target-count', label: '每日目标点击', children: `${values.daily_click_target_count || '-'} 次/日（命中目标即计）` },
          { key: 'membership-target-count', label: '每日成员关系观察目标', children: `${values.daily_target_count || '-'} 次/日（仅 membership_observed 计入）` },
          { key: 'repeat-application', label: '同账号重复申请', children: values.allow_same_account_repeat_application ? '允许' : '不允许' },
        ]),
        { key: 'account-group', label: '执行账号组', children: accountPool ? `${accountPool.name}（${accountPool.account_count} 个账号）` : '-' },
        { key: 'daily-limit', label: '每天 action 上限', children: `${values.max_actions_per_day || '-'} 次` },
        { key: 'end', label: '完成截止时间', children: values.scheduled_end ? formatDateTime(values.scheduled_end) : '-' },
        { key: 'jitter', label: '抖动', children: `日 ${values.daily_jitter_percent ?? 0}% / 每小时 ${values.hourly_jitter_percent ?? 0}%` },
        { key: 'quiet', label: '静默时间', children: quietHours },
        { key: 'policy', label: '系统托管', children: '代理、机器人、账号资格、真实结果与风控由系统管理' },
        { key: 'start', label: '创建说明', children: taskType === 'search_rank_deboost' ? '创建为草稿；启动准备时系统检查可用资源和执行条件。' : '创建并启动后由系统按可用资源执行。' },
      ]} />
    </Space>
  );
}

export function WizardReview({ taskType, values, accounts, accountPools, targets, ruleSets, slangTemplates, precheck, loading }: { taskType: TaskCenterTaskType; values: Record<string, any>; accounts: Account[]; accountPools: AccountPool[]; targets: OperationTarget[]; ruleSets: RuleSet[]; slangTemplates: PromptTemplate[]; precheck: TaskPrecheck | null; loading: boolean }) {
  if (taskType === 'search_join_group' || taskType === 'search_rank_deboost') {
    return <SimpleSearchClickReview taskType={taskType} values={values} targets={targets} accountPools={accountPools} />;
  }
  const account = accountPrecheck(values, accounts, accountPools, taskType);
  const profile = currentOperationProfile(values);
  const selectedSlang = slangTemplates.find((template) => template.id === values.slang_prompt_template_id);
  const displayTarget = targetName(values, targets);
  const targetSummary = taskType === 'group_relay'
    ? values.target_operation_target_ids?.length
      ? `运营目标 #${values.target_operation_target_id || '-'} + ${values.target_operation_target_ids.length} 个附加目标`
      : `运营目标 #${values.target_operation_target_id || '-'}`
    : values.target_operation_target_id
      ? `运营目标 #${values.target_operation_target_id}`
      : values.target_channel_id
        ? `频道 #${values.target_channel_id}`
        : '-';
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
