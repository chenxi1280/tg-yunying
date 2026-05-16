import React from 'react';
import { Alert, Checkbox, Collapse, Descriptions, Form, Input, InputNumber, Select, Space, Typography } from 'antd';
import type { Account, AccountPool, ChannelMessage, ChannelMessageComment, OperationTarget, PromptTemplate, RuleSet, TaskCenterTaskType, TaskPrecheck } from '../types';
import { TASK_TYPES, TYPE_LABEL, OPERATION_PROFILE_TEMPLATES, type OperationProfileTemplateId, accountPrecheck, csvNumbers, curveNumbers, curveText, currentOperationProfile, formatDateTime, operationProfileSummary, operationTemplate, ruleSummary, targetName } from './taskCenterViewModel';

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
      <Form.Item name="scheduled_end" label="结束时间（可选）"><Input type="datetime-local" placeholder="不填则持续运行" /></Form.Item>
    </div>
  );
}

export function WizardTarget({ taskType, groupTargets, channelTargets, messages, messageScope, targetChannelId, onTargetChannelChange }: { taskType: TaskCenterTaskType; groupTargets: OperationTarget[]; channelTargets: OperationTarget[]; messages: ChannelMessage[]; messageScope: string; targetChannelId?: number; onTargetChannelChange: () => void }) {
  const groupTargetOptions = groupTargets
    .filter((target) => target.auth_status === '已授权运营')
    .map((target) => ({
      value: target.id,
      label: `${target.title} / 可发账号 ${target.available_send_account_count} / 监听账号 ${target.listener_account_count}`,
    }));
  const sendableGroupTargetOptions = groupTargetOptions.filter((option) => groupTargets.find((target) => target.id === option.value)?.can_send);
  if (taskType === 'group_ai_chat') {
    return <div className="form-grid"><Form.Item name="target_operation_target_id" label="运营目标群" rules={[{ required: true }]}><Select options={sendableGroupTargetOptions} /></Form.Item></div>;
  }
  if (taskType === 'group_relay') {
    return (
      <div className="form-grid">
        <Form.Item name="source_operation_target_ids" label="源群运营目标" rules={[{ required: true }]}><Select mode="multiple" options={groupTargetOptions} /></Form.Item>
        <Form.Item name="target_operation_target_id" label="默认目标群" rules={[{ required: true }]}><Select options={sendableGroupTargetOptions} /></Form.Item>
        <Form.Item name="target_operation_target_ids" label="附加目标群"><Select mode="multiple" allowClear options={sendableGroupTargetOptions} /></Form.Item>
      </div>
    );
  }
  const scopedMessages = messages.filter((message) => !targetChannelId || message.channel_target_id === targetChannelId);
  return (
    <div className="form-grid">
      <Form.Item name="target_channel_id" label="目标频道" rules={[{ required: true }]}><Select options={channelTargets.map((target) => ({ value: target.id, label: target.title }))} onChange={onTargetChannelChange} /></Form.Item>
      <Form.Item name="message_scope" label="消息范围"><Select options={[{ value: 'dynamic_new', label: '持续监听新消息' }, { value: 'latest_n', label: '最新 N 条' }, { value: 'all', label: '所有消息' }, { value: 'date_range', label: '日期范围' }, { value: 'specific', label: '指定消息' }]} /></Form.Item>
      {['latest_n', 'dynamic_new'].includes(messageScope) && <Form.Item name="message_count" label={messageScope === 'dynamic_new' ? '每轮采集上限' : '消息数量'} rules={[{ required: true }]}><InputNumber min={1} max={500} /></Form.Item>}
      {messageScope === 'specific' && <Form.Item name="message_ids" label="频道消息" rules={[{ required: true }]}><Select mode="multiple" options={scopedMessages.map((message) => ({ value: message.id, label: `#${message.message_id} / ${message.content_preview || message.message_url || message.id}` }))} /></Form.Item>}
      {messageScope === 'date_range' && <><Form.Item name="date_from" label="开始时间"><Input type="datetime-local" /></Form.Item><Form.Item name="date_to" label="结束时间"><Input type="datetime-local" /></Form.Item></>}
    </div>
  );
}

export function WizardTypeConfig({
  taskType,
  ruleSets = [],
  slangTemplates = [],
  comments = [],
  targetChannelId,
  messageScope = 'latest_n',
  messageIds,
}: {
  taskType: TaskCenterTaskType;
  ruleSets?: RuleSet[];
  slangTemplates?: PromptTemplate[];
  comments?: ChannelMessageComment[];
  targetChannelId?: number;
  messageScope?: string;
  messageIds?: Array<number | string> | string | null;
}) {
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
        {ruleFields}
        <div className="form-grid">
          <Form.Item name="topic_hint" label="话题方向（可选）"><Input.TextArea rows={2} placeholder="不填时系统会按群目标方向或自然开场自动起聊" /></Form.Item>
          <Form.Item name="tone" label="语气"><Select options={[{ value: 'auto', label: '自动' }, { value: 'casual', label: '口语' }, { value: 'professional', label: '正式' }, { value: 'mixed', label: '混合' }]} /></Form.Item>
          <Form.Item name="slang_prompt_template_id" label="AI 黑话配置">
            <Select allowClear options={slangOptions} placeholder="选择系统设置里的 AI 黑话词表" />
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
                  <Form.Item name="messages_per_round_mode" label="每轮发言"><Select options={[{ value: 'auto', label: '系统自动判定' }, { value: 'manual', label: '手动指定' }]} /></Form.Item>
                  <Form.Item name="messages_per_round" label="手动每轮发言数"><InputNumber min={1} max={10} /></Form.Item>
                  <Form.Item name="chat_history_depth" label="历史条数"><InputNumber min={1} max={200} /></Form.Item>
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
      </Space>
    );
  }
  if (taskType === 'channel_view') {
    return (
      <Space direction="vertical" style={{ width: '100%' }}>
        <div className="form-grid"><Form.Item name="target_views_per_message" label="预计每条浏览"><InputNumber min={1} /></Form.Item></div>
        <Collapse ghost items={[{ key: 'advanced', label: '高级设置', children: <div className="form-grid"><Form.Item name="execution_mode" label="执行模式"><Select options={[{ value: 'distribute', label: '均匀分配' }, { value: 'burst', label: '尽快完成' }]} /></Form.Item></div> }]} />
      </Space>
    );
  }
  if (taskType === 'channel_like') {
    return (
      <Space direction="vertical" style={{ width: '100%' }}>
        <div className="form-grid"><Form.Item name="target_likes_per_message" label="预计每条点赞"><InputNumber min={1} /></Form.Item><Form.Item name="allowed_reactions" label="Reaction 范围"><Input /></Form.Item></div>
        <Collapse ghost items={[{ key: 'advanced', label: '高级设置', children: <div className="form-grid"><Form.Item name="max_likes_per_account_per_hour" label="每号每小时点赞上限"><InputNumber min={1} /></Form.Item></div> }]} />
      </Space>
    );
  }
  const selectedMessageIds = new Set(csvNumbers(messageIds));
  const commentOptions = comments
    .filter((comment) => {
      if (targetChannelId && comment.channel_target_id !== targetChannelId) return false;
      if (messageScope === 'specific') return selectedMessageIds.size > 0 && selectedMessageIds.has(comment.channel_message_id);
      return true;
    })
    .map((comment) => ({
      value: comment.comment_message_id,
      label: `消息#${comment.channel_message_id} / 评论#${comment.comment_message_id} / ${comment.author_name || '未知用户'} / ${comment.content_preview || '无内容预览'}`,
    }));
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <div style={{ gridColumn: '1 / -1' }}>
        <Alert type="info" showIcon message="AI 评论会按绑定规则集逐条做输出校验，单条失败不会废弃整批评论。" />
      </div>
      <div className="form-grid">
        <div style={{ gridColumn: '1 / -1' }}>{ruleFields}</div>
        <Form.Item name="target_comments_per_message" label="预计每条评论/回复"><InputNumber min={1} /></Form.Item>
        <Form.Item name="comment_mode" label="互动方式"><Select options={[{ value: 'comment', label: '评论频道消息' }, { value: 'reply', label: '回复指定评论' }, { value: 'mixed', label: '评论+回复' }]} /></Form.Item>
        <Form.Item name="reply_to_message_ids" label="回复对象">
          <Select
            mode="multiple"
            allowClear
            showSearch
            placeholder="选择当前频道消息下已采集评论"
            options={commentOptions}
          />
        </Form.Item>
        <Form.Item name="comment_style" label="评论方向"><Select options={[{ value: 'mixed', label: '混合' }, { value: 'relevant', label: '相关' }, { value: 'question', label: '提问' }, { value: 'praise', label: '正向' }, { value: 'discussion', label: '讨论' }]} /></Form.Item>
        <Form.Item name="topic_hint" label="主题方向"><Input /></Form.Item>
      </div>
      <Collapse ghost items={[{ key: 'advanced', label: '高级设置', children: <div className="form-grid"><Form.Item name="max_comments_per_account_per_hour" label="每号每小时评论上限"><InputNumber min={1} /></Form.Item><Form.Item name="system_prompt_override" label="System Prompt 覆盖"><Input.TextArea rows={3} /></Form.Item><Form.Item name="max_comment_length" label="最大评论长度"><InputNumber min={1} /></Form.Item></div> }]} />
    </Space>
  );
}

export function WizardOperationProfile({ form, values }: { form: any; values: Record<string, any> }) {
  const selectedTemplate = operationTemplate(values.operation_template_id);
  const curve = curveNumbers(values.hourly_activity_curve ?? selectedTemplate.curve);
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Alert type="info" showIcon message={`预计运行摘要：${operationProfileSummary(values)}。系统会按曲线、账号容量和风控自动推导静默、低频、爬坡和收尾。`} />
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
                <Form.Item name="hourly_activity_curve" label="小时强度">
                  <Input.TextArea
                    rows={3}
                    onChange={() => form.setFieldsValue({ operation_profile_manual_override: true })}
                  />
                </Form.Item>
                <Form.Item name="quiet_threshold" label="低频阈值"><InputNumber min={0} max={100} /></Form.Item>
                <Form.Item name="peak_threshold" label="高峰阈值"><InputNumber min={0} max={100} /></Form.Item>
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
      <Form.Item name="max_concurrent" label="最大并发"><InputNumber min={1} max={500} /></Form.Item>
      <Form.Item name="cooldown_per_account_minutes" label="账号冷却分钟"><InputNumber min={0} /></Form.Item>
      <Form.Item name="ban_policy" label="异常账号处理"><Select options={[{ value: 'skip', label: '跳过账号' }, { value: 'pause_task', label: '暂停任务' }, { value: 'alert', label: '只告警' }]} /></Form.Item>
      <Form.Item name="max_actions_per_hour" label="每小时上限"><InputNumber min={1} /></Form.Item>
      <Form.Item name="max_actions_per_day" label="每日上限"><InputNumber min={1} /></Form.Item>
      <Form.Item name="max_retries" label="失败重试次数"><InputNumber min={0} max={10} /></Form.Item>
    </>
  );
}

export function WizardAccounts({ accountMode, accounts, accountPools }: { accountMode: string; accounts: Account[]; accountPools: AccountPool[] }) {
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
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      {precheck && (
        <Alert
          type={precheck.decision === 'block' ? 'error' : precheck.decision === 'warn' ? 'warning' : 'success'}
          showIcon
          message={`创建前预检：${precheckStatus}`}
          description={[...precheck.blockers, ...precheck.warnings, ...precheck.risk_hits].filter(Boolean).slice(0, 5).join('；') || '账号、目标、规则和风控检查通过'}
        />
      )}
      <Descriptions bordered column={2} size="small" items={[
      { key: 'type', label: '任务类型', children: TYPE_LABEL[taskType] },
      { key: 'name', label: '任务名称', children: values.name || '-' },
      { key: 'end', label: '结束时间', children: values.scheduled_end ? formatDateTime(values.scheduled_end) : '不限制' },
      { key: 'target', label: '任务目标', children: displayTarget === '-' ? targetSummary : displayTarget },
      { key: 'account', label: '账号摘要', children: precheck ? `候选 ${precheck.candidate_account_count} 个，可用 ${precheck.available_account_count} 个，受限 ${precheck.limited_account_count} 个，阻塞 ${precheck.blocked_account_count} 个` : `${account.label}，候选 ${account.total} 个，当前在线 ${account.online} 个，受限/离线 ${account.limited} 个` },
      { key: 'targetAbility', label: '目标能力', children: precheck?.target_ability?.length ? precheck.target_ability.map((item) => `${item.title || item.target_id} / ${item.can_task ? '可创建任务' : item.auth_status || '不可用'}`).join('；') : displayTarget },
      { key: 'estimate', label: '预计动作量', children: precheck ? `预计 ${precheck.estimated_actions} 条，容量缺口 ${precheck.capacity_shortfall}` : '等待预检' },
      { key: 'pacing', label: '曲线摘要', children: `${operationProfileSummary(values)}；当前 ${String(profile.hour).padStart(2, '0')}:00 强度 ${profile.intensity}，${profile.mode}运行` },
      { key: 'rule', label: '规则版本', children: precheck?.rule_version ? `规则集 #${precheck.rule_version.rule_set_id} / v${precheck.rule_version.version} / ${precheck.rule_version.status}` : ['group_relay', 'group_ai_chat', 'channel_comment'].includes(taskType) ? ruleSummary(values, ruleSets) : '平台默认规则' },
      { key: 'ai', label: 'AI 摘要', children: taskType === 'group_ai_chat' ? `语气 ${values.tone || 'auto'}，黑话集 ${selectedSlang ? `${selectedSlang.name} / v${selectedSlang.version}` : '系统默认语气'}` : taskType === 'channel_comment' ? `评论方向 ${values.comment_style || 'mixed'}，主题 ${values.topic_hint || '按消息内容'}` : '-' },
      { key: 'risk', label: '风控命中', children: precheck?.risk_hits?.length ? precheck.risk_hits.join('；') : `每小时上限 ${values.max_actions_per_hour || '按系统默认'}，每日上限 ${values.max_actions_per_day || '按系统默认'}，失败重试 ${values.max_retries ?? 3} 次` },
      { key: 'blockers', label: '阻塞项', children: precheck?.blockers?.length ? precheck.blockers.join('；') : '无' },
      { key: 'mode', label: '启动说明', children: precheck?.decision === 'block' ? '当前预检存在阻塞项，需处理后再启动。' : account.online > 0 ? '创建后 worker 会再次校验账号、目标、规则和风控，再按曲线执行。' : '当前账号范围没有在线账号，创建后会等待账号恢复。' },
    ]} />
    </Space>
  );
}
