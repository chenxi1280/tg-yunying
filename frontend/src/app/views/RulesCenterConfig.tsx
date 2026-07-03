import React from 'react';
import { Alert, Button, Card, Form, Input, InputNumber, Select } from 'antd';
import type { OperationTarget, RuleSet } from '../types';

function defaultRuleJson() {
  return {
    filters: '{}',
    output_checks: '{"failure_strategy":"transform_once_drop"}',
    transforms: '{}',
    routing: '{}',
    account_strategy: '{"mode":"target_sticky"}',
    rate_limits: '{}',
    retry_policy: '{"max_retries":3}',
  };
}

export const TASK_TYPE_OPTIONS = [
  { value: 'group_relay', label: '监听转发' },
  { value: 'group_ai_chat', label: 'AI 回复' },
  { value: 'channel_comment', label: 'AI 评论' },
  { value: 'search_join_group', label: '搜索目标群点击任务' },
  { value: 'message_send', label: '普通消息发送' },
];

export const TEST_MODE_OPTIONS = [
  { value: 'rules_only', label: '仅规则测试' },
  { value: 'ai_dry_run', label: 'AI 干跑测试' },
  { value: 'history_replay', label: '历史样本回放' },
];

export const MEDIA_SIMULATION_OPTIONS = [
  { value: '', label: '不模拟媒体缓存' },
  { value: 'pending_cache', label: '待缓存' },
  { value: 'timeout_then_cached', label: '超时后缓存完成' },
  { value: 'late_cache_event', label: '旧事件迟到' },
  { value: 'album_one_failed', label: '相册一张失败' },
  { value: 'queue_overflow', label: '等待队列超量' },
];

const OUTPUT_FAILURE_OPTIONS = [
  { value: 'transform_once_drop', label: '转换一次，仍失败则丢弃' },
  { value: 'drop', label: '直接丢弃' },
  { value: 'rewrite_once', label: '重新生成一次' },
  { value: 'fixed_reply', label: '固定回复' },
];

export function taskTypeLabels(values: string[] | undefined) {
  const labels = new Map(TASK_TYPE_OPTIONS.map((item) => [item.value, item.label]));
  return (values ?? []).map((value) => labels.get(value) ?? value);
}

export function defaultRuleFormValues() {
  return {
    ...defaultRuleJson(),
    visual_keyword_whitelist: '',
    visual_keyword_blacklist: '',
    visual_min_message_length: null,
    visual_max_message_length: null,
    visual_allowed_media_types: '',
    visual_blocked_user_ids: '',
    visual_message_type_filter: 'all',
    visual_expression_mode: 'all',
    visual_expression_conditions: '',
    visual_prefix: '',
    visual_suffix: '',
    visual_remove_mentions: false,
    visual_remove_links: false,
    visual_default_operation_target_ids: [],
    visual_default_target_group_ids: '',
    visual_source_group_map: '',
    visual_keyword_routes: '',
    visual_routes: '',
    visual_account_mode: 'target_sticky',
    visual_fixed_account_id: null,
    visual_account_weights: '',
    visual_per_target_per_hour: null,
    visual_cooldown_seconds: null,
    visual_max_retries: 3,
    task_types: ['group_relay'],
    input_failure: 'skip',
    output_failure: 'transform_once_drop',
    version_binding: 'follow_current',
    version_note: '配置编辑自动生成',
    publish_reason: '',
    visual_forbidden_keywords: '',
    visual_forbid_links: false,
    visual_forbid_mentions: true,
    visual_forbid_contacts: false,
    visual_output_min_length: null,
    visual_output_max_length: null,
    visual_output_failure_strategy: 'transform_once_drop',
    visual_material_enabled: false,
    visual_material_type: '表情包',
    visual_material_tags: '',
    visual_material_action: 'append_media',
    visual_material_fallback: 'text_only',
  };
}

export function ruleFormValuesFromVersion(ruleSet: RuleSet, groupTargets: OperationTarget[] = [], sourceVersion?: RuleSet['versions'][number]) {
  const version = sourceVersion ?? ruleSet.versions.find((item) => item.id === ruleSet.active_version_id) ?? ruleSet.versions[0];
  if (!version) return defaultRuleFormValues();
  const filters = version.filters ?? {};
  const transforms = version.transforms ?? {};
  const outputChecks = version.output_checks ?? {};
  const routing = version.routing ?? {};
  const materialPolicy = routing.material_policy ?? {};
  const defaultGroupIds = numberList(routing.target_group_ids ?? routing.default_target_group_ids);
  const mappedTargetIds = operationTargetIdsForGroupIds(defaultGroupIds, groupTargets);
  const unmappedGroupIds = defaultGroupIds.filter((id) => !groupTargets.some((target) => target.linked_group_id === id));
  const accountStrategy = version.account_strategy ?? {};
  const rateLimits = version.rate_limits ?? {};
  const retryPolicy = version.retry_policy ?? {};
  return {
    filters: formatJson(filters),
    output_checks: formatJson(outputChecks),
    transforms: formatJson(transforms),
    routing: formatJson(routing),
    account_strategy: formatJson(accountStrategy),
    rate_limits: formatJson(rateLimits),
    retry_policy: formatJson(retryPolicy),
    visual_keyword_whitelist: (filters.keyword_whitelist ?? []).join(','),
    visual_keyword_blacklist: (filters.keyword_blacklist ?? []).join(','),
    visual_min_message_length: filters.min_message_length ?? null,
    visual_max_message_length: filters.max_message_length ?? null,
    visual_allowed_media_types: (filters.allowed_media_types ?? []).join(','),
    visual_blocked_user_ids: (filters.blocked_user_ids ?? []).join(','),
    visual_message_type_filter: filters.only_with_media ? 'media' : filters.only_text ? 'text' : 'all',
    visual_expression_mode: filters.expression?.mode ?? filters.expression?.logic ?? 'all',
    visual_expression_conditions: formatFilterExpression(filters.expression),
    visual_prefix: transforms.prefix ?? '',
    visual_suffix: transforms.suffix ?? '',
    visual_remove_mentions: Boolean(transforms.remove_mentions),
    visual_remove_links: Boolean(transforms.remove_links),
    visual_default_operation_target_ids: mappedTargetIds,
    visual_default_target_group_ids: unmappedGroupIds.join(','),
    visual_source_group_map: formatSourceGroupMap(routing.source_group_map ?? routing.source_to_targets),
    visual_keyword_routes: formatKeywordRoutes(routing.keyword_routes),
    visual_routes: formatRoutes(routing.routes),
    visual_material_enabled: Boolean(materialPolicy.enabled),
    visual_material_type: materialPolicy.material_type ?? '表情包',
    visual_material_tags: words(materialPolicy.required_tags ?? materialPolicy.tags).join(','),
    visual_material_action: materialPolicy.action ?? 'append_media',
    visual_material_fallback: materialPolicy.fallback ?? 'text_only',
    visual_account_mode: accountStrategy.mode ?? 'target_sticky',
    visual_fixed_account_id: accountStrategy.account_id ?? accountStrategy.fixed_account_id ?? null,
    visual_account_weights: formatAccountWeights(accountStrategy.weights),
    visual_per_target_per_hour: rateLimits.per_target_per_hour ?? null,
    visual_cooldown_seconds: rateLimits.cooldown_seconds ?? null,
    visual_max_retries: retryPolicy.max_retries ?? 3,
    visual_forbidden_keywords: (outputChecks.forbidden_keywords ?? outputChecks.blocked_keywords ?? []).join(','),
    visual_forbid_links: Boolean(outputChecks.forbid_links ?? outputChecks.no_links),
    visual_forbid_mentions: Boolean(outputChecks.forbid_mentions ?? outputChecks.no_mentions),
    visual_forbid_contacts: Boolean(outputChecks.forbid_contacts ?? outputChecks.no_contacts),
    visual_output_min_length: outputChecks.min_length ?? null,
    visual_output_max_length: outputChecks.max_length ?? null,
    visual_output_failure_strategy: outputChecks.failure_strategy ?? outputChecks.on_failure ?? 'transform_once_drop',
    version_note: version.version_note ?? '',
    publish_reason: '',
  };
}

export function preferredRuleSet(ruleSets: RuleSet[]) {
  return ruleSets.find((item) => item.name.includes('默认')) ?? ruleSets[0];
}

export function preferredVersion(ruleSet: RuleSet) {
  return ruleSet.versions.find((item) => item.id === ruleSet.active_version_id)
    ?? ruleSet.versions.find((item) => item.status === 'published')
    ?? ruleSet.versions[0];
}

export function composeRuleConfig(values: Record<string, any>, groupTargets: OperationTarget[]) {
  const filters = readStrictJsonObject(values.filters, 'filters');
  const outputChecks = readStrictJsonObject(values.output_checks, 'output_checks');
  const transforms = readStrictJsonObject(values.transforms, 'transforms');
  const routing = readStrictJsonObject(values.routing, 'routing');
  const accountStrategy = readStrictJsonObject(values.account_strategy, 'account_strategy');
  const rateLimits = readStrictJsonObject(values.rate_limits, 'rate_limits');
  const retryPolicy = readStrictJsonObject(values.retry_policy, 'retry_policy');

  filters.keyword_whitelist = words(values.visual_keyword_whitelist);
  filters.keyword_blacklist = words(values.visual_keyword_blacklist);
  setOptionalNumber(filters, 'min_message_length', values.visual_min_message_length);
  setOptionalNumber(filters, 'max_message_length', values.visual_max_message_length);
  setOptionalList(filters, 'allowed_media_types', words(values.visual_allowed_media_types));
  setOptionalList(filters, 'blocked_user_ids', words(values.visual_blocked_user_ids));
  filters.only_with_media = values.visual_message_type_filter === 'media';
  filters.only_text = values.visual_message_type_filter === 'text';
  const expression = parseFilterExpression(values.visual_expression_mode, values.visual_expression_conditions);
  if (expression.conditions.length) {
    filters.expression = expression;
  } else {
    delete filters.expression;
  }

  transforms.prefix = values.visual_prefix || '';
  transforms.suffix = values.visual_suffix || '';
  transforms.remove_mentions = Boolean(values.visual_remove_mentions);
  transforms.remove_links = Boolean(values.visual_remove_links);

  outputChecks.forbidden_keywords = words(values.visual_forbidden_keywords);
  outputChecks.forbid_links = Boolean(values.visual_forbid_links);
  outputChecks.forbid_mentions = Boolean(values.visual_forbid_mentions);
  outputChecks.forbid_contacts = Boolean(values.visual_forbid_contacts);
  outputChecks.failure_strategy = values.visual_output_failure_strategy || 'transform_once_drop';
  setOptionalNumber(outputChecks, 'min_length', values.visual_output_min_length);
  setOptionalNumber(outputChecks, 'max_length', values.visual_output_max_length);

  routing.target_group_ids = uniqueNumbers([
    ...groupIdsForOperationTargetIds(values.visual_default_operation_target_ids, groupTargets),
    ...numberList(values.visual_default_target_group_ids),
  ]);
  const sourceGroupMap = parseSourceGroupMap(values.visual_source_group_map);
  if (Object.keys(sourceGroupMap).length) {
    routing.source_group_map = sourceGroupMap;
  } else {
    delete routing.source_group_map;
  }
  const keywordRoutes = parseKeywordRoutes(values.visual_keyword_routes);
  if (keywordRoutes.length) {
    routing.keyword_routes = keywordRoutes;
  } else {
    delete routing.keyword_routes;
  }
  const routes = parseRoutes(values.visual_routes);
  if (routes.length) {
    routing.routes = routes;
  } else {
    delete routing.routes;
  }
  if (values.visual_material_enabled) {
    routing.material_policy = {
      enabled: true,
      material_type: values.visual_material_type || '表情包',
      required_tags: words(values.visual_material_tags),
      action: values.visual_material_action || 'append_media',
      fallback: values.visual_material_fallback || 'text_only',
    };
  } else {
    delete routing.material_policy;
  }

  accountStrategy.mode = values.visual_account_mode || 'target_sticky';
  if (values.visual_fixed_account_id) {
    accountStrategy.account_id = values.visual_fixed_account_id;
  } else {
    delete accountStrategy.account_id;
    delete accountStrategy.fixed_account_id;
  }
  const accountWeights = parseAccountWeights(values.visual_account_weights);
  if (Object.keys(accountWeights).length) {
    accountStrategy.weights = accountWeights;
  } else {
    delete accountStrategy.weights;
  }

  if (values.visual_per_target_per_hour) {
    rateLimits.per_target_per_hour = values.visual_per_target_per_hour;
  } else {
    delete rateLimits.per_target_per_hour;
  }
  if (values.visual_cooldown_seconds) {
    rateLimits.cooldown_seconds = values.visual_cooldown_seconds;
  } else {
    delete rateLimits.cooldown_seconds;
  }
  retryPolicy.max_retries = values.visual_max_retries ?? 3;

  return {
    filters,
    output_checks: outputChecks,
    transforms,
    routing,
    account_strategy: accountStrategy,
    rate_limits: rateLimits,
    retry_policy: retryPolicy,
  };
}

export function RuleSetForm({ form, includeBasics = false, groupTargets = [] }: { form: ReturnType<typeof Form.useForm>[0]; includeBasics?: boolean; groupTargets?: OperationTarget[] }) {
  const operationTargetOptions = groupTargets.map((target) => ({
    value: target.id,
    label: `${target.title} / 目标#${target.id} / 群#${target.linked_group_id}`,
  }));

  function applyVisualTemplate() {
    const values = form.getFieldsValue() as Record<string, any>;
    const config = composeRuleConfig(values, groupTargets);

    form.setFieldsValue({
      filters: formatJson(config.filters),
      output_checks: formatJson(config.output_checks),
      transforms: formatJson(config.transforms),
      routing: formatJson(config.routing),
      account_strategy: formatJson(config.account_strategy),
      rate_limits: formatJson(config.rate_limits),
      retry_policy: formatJson(config.retry_policy),
    });
  }

  return (
    <Form form={form} layout="vertical" initialValues={defaultRuleFormValues()}>
      {includeBasics && (
        <div className="form-grid">
          <Form.Item name="name" label="规则集名称" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="description" label="说明"><Input /></Form.Item>
          <Form.Item name="task_types" label="适用任务类型" rules={[{ required: true }]}><Select mode="multiple" options={TASK_TYPE_OPTIONS} /></Form.Item>
          <Form.Item name="input_failure" label="输入失败处理"><Select options={[{ value: 'skip', label: '跳过' }, { value: 'block', label: '拦截' }, { value: 'mark', label: '标记风险' }]} /></Form.Item>
          <Form.Item name="output_failure" label="输出失败处理"><Select options={OUTPUT_FAILURE_OPTIONS} /></Form.Item>
          <Form.Item name="version_binding" label="任务绑定方式"><Select options={[{ value: 'fixed_version', label: '固定版本' }, { value: 'follow_current', label: '跟随当前发布版本' }]} /></Form.Item>
        </div>
      )}
      <Form.Item name="version_note" label="版本说明"><Input placeholder="例如：调整关键词、输出校验或路由策略" /></Form.Item>
      {!includeBasics && (
        <Form.Item name="publish_reason" label="发布原因" rules={[{ required: true, whitespace: true, message: '请输入发布原因' }]}>
          <Input.TextArea rows={2} placeholder="说明为什么发布此配置，会写入审计记录" />
        </Form.Item>
      )}
      <Card size="small" title="规则配置" extra={<Button size="small" onClick={applyVisualTemplate}>同步高级配置</Button>}>
        <div className="form-grid">
          <Form.Item name="visual_keyword_whitelist" label="白名单关键词"><Input placeholder="公告, 活动" /></Form.Item>
          <Form.Item name="visual_keyword_blacklist" label="黑名单关键词"><Input placeholder="广告, 禁止" /></Form.Item>
          <Form.Item name="visual_min_message_length" label="最小长度"><InputNumber min={0} style={{ width: '100%' }} /></Form.Item>
          <Form.Item name="visual_max_message_length" label="最大长度"><InputNumber min={1} style={{ width: '100%' }} /></Form.Item>
          <Form.Item name="visual_allowed_media_types" label="允许媒体类型"><Input placeholder="text, photo, video" /></Form.Item>
          <Form.Item name="visual_blocked_user_ids" label="屏蔽用户 ID"><Input placeholder="12345, 67890" /></Form.Item>
          <Form.Item name="visual_message_type_filter" label="消息类型"><Select options={[{ value: 'all', label: '不限' }, { value: 'text', label: '仅文本' }, { value: 'media', label: '仅媒体' }]} /></Form.Item>
          <Form.Item name="visual_expression_mode" label="组合条件模式"><Select options={[{ value: 'all', label: '全部满足' }, { value: 'any', label: '任一满足' }]} /></Form.Item>
          <Form.Item name="visual_expression_conditions" label="组合条件">
            <Input.TextArea rows={3} placeholder="content contains 公告,活动&#10;content not_contains 禁止&#10;length gte 10" />
          </Form.Item>
          <Form.Item name="visual_prefix" label="转发前缀"><Input /></Form.Item>
          <Form.Item name="visual_suffix" label="转发后缀"><Input /></Form.Item>
          <Form.Item name="visual_remove_mentions" label="@ 提及"><Select options={[{ value: false, label: '保留' }, { value: true, label: '移除' }]} /></Form.Item>
          <Form.Item name="visual_remove_links" label="链接"><Select options={[{ value: false, label: '保留' }, { value: true, label: '移除' }]} /></Form.Item>
          <Form.Item name="visual_forbidden_keywords" label="输出禁止关键词"><Input placeholder="引流, 联系方式" /></Form.Item>
          <Form.Item name="visual_forbid_links" label="输出链接"><Select options={[{ value: false, label: '允许' }, { value: true, label: '禁止' }]} /></Form.Item>
          <Form.Item name="visual_forbid_mentions" label="输出 @"><Select options={[{ value: true, label: '禁止' }, { value: false, label: '允许' }]} /></Form.Item>
          <Form.Item name="visual_forbid_contacts" label="联系方式"><Select options={[{ value: false, label: '允许' }, { value: true, label: '禁止' }]} /></Form.Item>
          <Form.Item name="visual_output_min_length" label="输出最小长度"><InputNumber min={0} style={{ width: '100%' }} /></Form.Item>
          <Form.Item name="visual_output_max_length" label="输出最大长度"><InputNumber min={1} style={{ width: '100%' }} /></Form.Item>
          <Form.Item name="visual_output_failure_strategy" label="输出失败策略"><Select options={OUTPUT_FAILURE_OPTIONS} /></Form.Item>
          <Form.Item name="visual_default_operation_target_ids" label="默认运营目标">
            <Select mode="multiple" allowClear placeholder="选择转发目标" options={operationTargetOptions} />
          </Form.Item>
          <Form.Item name="visual_default_target_group_ids" label="兼容目标群 ID"><Input placeholder="仅旧数据或未建运营目标时填写，如 9, 10" /></Form.Item>
          <Form.Item name="visual_source_group_map" label="源群映射">
            <Input.TextArea rows={2} placeholder="7 -> 9,10&#10;8 -> 11" />
          </Form.Item>
          <Form.Item name="visual_keyword_routes" label="关键词路由">
            <Input.TextArea rows={2} placeholder="公告 -> 9,10&#10;活动,报名 -> 11" />
          </Form.Item>
          <Form.Item name="visual_routes" label="源群+关键词路由">
            <Input.TextArea rows={2} placeholder="7 | 公告,活动 -> 9,10&#10;8 | 报名 -> 11" />
          </Form.Item>
          <Form.Item name="visual_material_enabled" label="素材动作"><Select options={[{ value: false, label: '不追加素材' }, { value: true, label: '按规则选择素材' }]} /></Form.Item>
          <Form.Item name="visual_material_type" label="素材类型"><Select options={['图片', '表情包', '文件'].map((value) => ({ value, label: value }))} /></Form.Item>
          <Form.Item name="visual_material_tags" label="素材标签"><Input placeholder="围观, 欢迎" /></Form.Item>
          <Form.Item name="visual_material_action" label="素材发送方式"><Select options={[{ value: 'append_media', label: '追加素材' }, { value: 'replace_media', label: '替换源媒体' }]} /></Form.Item>
          <Form.Item name="visual_material_fallback" label="素材不可用"><Select options={[{ value: 'text_only', label: '只发文字' }, { value: 'skip', label: '跳过本条' }]} /></Form.Item>
          <Form.Item name="visual_account_mode" label="账号策略"><Select options={[{ value: 'target_sticky', label: '目标群粘性' }, { value: 'source_target_sticky', label: '源群+目标群粘性' }, { value: 'round_robin', label: '轮询' }, { value: 'random', label: '随机' }, { value: 'weighted_random', label: '权重随机' }, { value: 'fixed', label: '固定账号' }]} /></Form.Item>
          <Form.Item name="visual_fixed_account_id" label="固定账号 ID"><InputNumber min={1} style={{ width: '100%' }} /></Form.Item>
          <Form.Item name="visual_account_weights" label="账号权重">
            <Input.TextArea rows={2} placeholder="101=5&#10;102=1" />
          </Form.Item>
          <Form.Item name="visual_per_target_per_hour" label="每目标每小时"><InputNumber min={1} style={{ width: '100%' }} /></Form.Item>
          <Form.Item name="visual_cooldown_seconds" label="冷却秒数"><InputNumber min={0} style={{ width: '100%' }} /></Form.Item>
          <Form.Item name="visual_max_retries" label="最大重试"><InputNumber min={0} max={10} style={{ width: '100%' }} /></Form.Item>
        </div>
      </Card>
      <div className="form-grid">
        <Form.Item name="filters" label="过滤规则 JSON"><Input.TextArea rows={4} /></Form.Item>
        <Form.Item name="output_checks" label="输出校验 JSON"><Input.TextArea rows={4} /></Form.Item>
        <Form.Item name="transforms" label="转换规则 JSON"><Input.TextArea rows={4} /></Form.Item>
        <Form.Item name="routing" label="路由规则 JSON"><Input.TextArea rows={4} /></Form.Item>
        <Form.Item name="account_strategy" label="账号策略 JSON"><Input.TextArea rows={4} /></Form.Item>
        <Form.Item name="rate_limits" label="限速策略 JSON"><Input.TextArea rows={4} /></Form.Item>
        <Form.Item name="retry_policy" label="重试策略 JSON"><Input.TextArea rows={4} /></Form.Item>
      </div>
    </Form>
  );
}

function readJsonObject(raw: string): Record<string, any> {
  try {
    const parsed = JSON.parse((raw || '').trim() || '{}');
    return parsed && !Array.isArray(parsed) && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

function readStrictJsonObject(raw: string, key: string): Record<string, any> {
  const parsed = JSON.parse((raw || '').trim() || '{}');
  if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') {
    throw new Error(`${key} 必须是 JSON 对象`);
  }
  return parsed;
}

export function formatJson(value: Record<string, any>) {
  return JSON.stringify(value, null, 2);
}

function setOptionalNumber(target: Record<string, any>, key: string, value: unknown) {
  const number = Number(value);
  if (Number.isFinite(number) && number >= 0) {
    target[key] = number;
  } else {
    delete target[key];
  }
}

function setOptionalList(target: Record<string, any>, key: string, value: string[]) {
  if (value.length) {
    target[key] = value;
  } else {
    delete target[key];
  }
}

function words(value: unknown): string[] {
  if (Array.isArray(value)) return value.map((item) => String(item).trim()).filter(Boolean);
  return String(value ?? '').split(/[,，\n\s]+/).map((item) => item.trim()).filter(Boolean);
}

function numberList(value: unknown): number[] {
  return words(value).map((item) => Number(item)).filter((item) => Number.isFinite(item) && item > 0);
}

function uniqueNumbers(values: number[]): number[] {
  return Array.from(new Set(values.filter((item) => Number.isFinite(item) && item > 0)));
}

function groupIdsForOperationTargetIds(value: unknown, groupTargets: OperationTarget[]): number[] {
  const ids = numberList(value);
  return uniqueNumbers(ids.flatMap((id) => {
    const target = groupTargets.find((item) => item.id === id);
    return target?.linked_group_id ? [target.linked_group_id] : [];
  }));
}

function operationTargetIdsForGroupIds(groupIds: number[], groupTargets: OperationTarget[]): number[] {
  return uniqueNumbers(groupIds.flatMap((groupId) => {
    const target = groupTargets.find((item) => item.linked_group_id === groupId);
    return target ? [target.id] : [];
  }));
}

function parseArrowLine(line: string): [string, string] | null {
  const [left, ...rightParts] = line.split(/->|=>|→/);
  const right = rightParts.join('->');
  if (!left?.trim() || !right?.trim()) return null;
  return [left.trim(), right.trim()];
}

function parseSourceGroupMap(value: unknown): Record<string, number[]> {
  const map: Record<string, number[]> = {};
  String(value ?? '').split(/\n+/).forEach((line) => {
    const parsed = parseArrowLine(line);
    if (!parsed) return;
    const sourceIds = numberList(parsed[0]);
    const targetIds = numberList(parsed[1]);
    sourceIds.forEach((sourceId) => {
      if (targetIds.length) map[String(sourceId)] = targetIds;
    });
  });
  return map;
}

function parseKeywordRoutes(value: unknown): Array<{ keywords: string[]; target_group_ids: number[] }> {
  return String(value ?? '').split(/\n+/).flatMap((line) => {
    const parsed = parseArrowLine(line);
    if (!parsed) return [];
    const keywords = words(parsed[0]);
    const targetGroupIds = numberList(parsed[1]);
    return keywords.length && targetGroupIds.length ? [{ keywords, target_group_ids: targetGroupIds }] : [];
  });
}

function parseRoutes(value: unknown): Array<{ source_group_ids: number[]; keywords: string[]; target_group_ids: number[] }> {
  return String(value ?? '').split(/\n+/).flatMap((line) => {
    const parsed = parseArrowLine(line);
    if (!parsed) return [];
    const [sourceRaw, keywordRaw = ''] = parsed[0].split('|');
    const sourceGroupIds = numberList(sourceRaw);
    const keywords = words(keywordRaw);
    const targetGroupIds = numberList(parsed[1]);
    return sourceGroupIds.length && targetGroupIds.length ? [{ source_group_ids: sourceGroupIds, keywords, target_group_ids: targetGroupIds }] : [];
  });
}

function parseFilterExpression(mode: unknown, value: unknown): { mode: string; conditions: Array<{ field: string; operator: string; value: string | string[] | number }> } {
  const conditions = String(value ?? '').split(/\n+/).flatMap((line) => {
    const cleaned = line.trim();
    if (!cleaned) return [];
    const match = cleaned.match(/^(\S+)\s+(\S+)\s+(.+)$/);
    if (!match) return [];
    const [, rawField, rawOperator, rawValue] = match;
    const field = normalizeExpressionField(rawField);
    const operator = normalizeExpressionOperator(rawOperator);
    const valueText = rawValue.trim();
    const parsedValue = field === 'length' ? Number(valueText) : words(valueText);
    if (field === 'length' && !Number.isFinite(parsedValue as number)) return [];
    return [{ field, operator, value: parsedValue }];
  });
  return { mode: String(mode || 'all'), conditions };
}

function normalizeExpressionField(value: string): string {
  const field = value.trim().toLowerCase();
  const map: Record<string, string> = {
    文本: 'content',
    内容: 'content',
    content: 'content',
    text: 'content',
    sender: 'sender_id',
    sender_id: 'sender_id',
    发送者: 'sender_id',
    type: 'message_type',
    message_type: 'message_type',
    类型: 'message_type',
    length: 'length',
    长度: 'length',
  };
  return map[field] || field;
}

function normalizeExpressionOperator(value: string): string {
  const operator = value.trim().toLowerCase();
  const map: Record<string, string> = {
    包含: 'contains',
    不包含: 'not_contains',
    等于: 'eq',
    不等于: 'neq',
    属于: 'in',
    不属于: 'not_in',
    至少: 'gte',
    至多: 'lte',
  };
  return map[operator] || operator;
}

function formatFilterExpression(expression: unknown): string {
  if (!expression || Array.isArray(expression) || typeof expression !== 'object') return '';
  const conditions = (expression as Record<string, any>).conditions;
  if (!Array.isArray(conditions)) return '';
  return conditions.map((condition) => {
    if (!condition || typeof condition !== 'object') return '';
    const value = Array.isArray(condition.value) ? condition.value.join(',') : String(condition.value ?? '');
    return `${condition.field || 'content'} ${condition.operator || 'contains'} ${value}`.trim();
  }).filter(Boolean).join('\n');
}

function parseAccountWeights(value: unknown): Record<string, number> {
  const weights: Record<string, number> = {};
  String(value ?? '').split(/\n+/).forEach((line) => {
    const [accountRaw, weightRaw] = line.split(/=|:|：/);
    const accountId = Number(accountRaw?.trim());
    const weight = Number(weightRaw?.trim());
    if (Number.isFinite(accountId) && accountId > 0 && Number.isFinite(weight) && weight > 0) {
      weights[String(accountId)] = Math.round(weight);
    }
  });
  return weights;
}

function formatSourceGroupMap(value: unknown): string {
  if (!value || Array.isArray(value) || typeof value !== 'object') return '';
  return Object.entries(value as Record<string, unknown>)
    .map(([sourceId, targetIds]) => `${sourceId} -> ${numberList(targetIds).join(',')}`)
    .filter((line) => !line.endsWith('-> '))
    .join('\n');
}

function formatKeywordRoutes(value: unknown): string {
  if (!Array.isArray(value)) return '';
  return value.map((route) => {
    const keywords = words(route?.keywords ?? route?.keyword).join(',');
    const targetIds = numberList(route?.target_group_ids ?? route?.targets).join(',');
    return keywords && targetIds ? `${keywords} -> ${targetIds}` : '';
  }).filter(Boolean).join('\n');
}

function formatRoutes(value: unknown): string {
  if (!Array.isArray(value)) return '';
  return value.map((route) => {
    const sourceIds = numberList(route?.source_group_ids ?? route?.source_groups).join(',');
    const keywords = words(route?.keywords ?? route?.keyword).join(',');
    const targetIds = numberList(route?.target_group_ids ?? route?.targets).join(',');
    return sourceIds && targetIds ? `${sourceIds} | ${keywords} -> ${targetIds}` : '';
  }).filter(Boolean).join('\n');
}

function formatAccountWeights(value: unknown): string {
  if (!value || Array.isArray(value) || typeof value !== 'object') return '';
  return Object.entries(value as Record<string, unknown>)
    .map(([accountId, weight]) => `${accountId}=${Number(weight)}`)
    .filter((line) => !line.endsWith('=NaN'))
    .join('\n');
}
