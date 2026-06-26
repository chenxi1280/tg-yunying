import type { Key } from 'react';
import { ApiError } from '../../shared/api/client';
import { formatBeijingDateTime } from '../time';

export type TargetProfileOverview = {
  profile_id: string;
  profile_version: number;
  status: string;
  learning_enabled: boolean;
  usage_scope: string[];
  style_summary: string;
  source_sample_count: number;
  source_count: number;
  quality_rule_version: number;
  last_rebuilt_at?: string | null;
  available_for_ai: boolean;
};

export type TargetProfileUsage = {
  running_task_count: number;
  task_type_distribution: Record<string, number>;
};

export type SourceCandidate = {
  source_key: string;
  group_id?: number | null;
  target_id?: number | null;
  target_type: string;
  title: string;
  tg_peer_id: string;
  can_listen: boolean;
  listener_account_ids: number[];
  recent_message_at?: string | null;
  recommended: boolean;
  recommend_reason: string;
  cannot_auto_sync_reason: string;
};

export type LearningSource = {
  id: string;
  target_id: number;
  target_title: string;
  target_type: string;
  is_enabled: boolean;
  auto_sync_enabled: boolean;
  source_status: string;
  listener_account_ids: number[];
  last_sync_at?: string | null;
  last_history_pull_at?: string | null;
  last_failure_detail: string;
};

export type LearningSample = {
  id: string;
  source_scene: string;
  sender_name: string;
  text: string;
  learning_status: string;
  quality_score: number;
  sent_at?: string | null;
};

export type LearningRun = {
  id: string;
  run_type: string;
  status: string;
  pulled_count: number;
  sample_count: number;
  accepted_count: number;
  rejected_count: number;
  profile_version?: number | null;
  quality_rule_version?: number | null;
  failure_detail: string;
  created_at?: string | null;
};

export type ProfileVersion = {
  id: string;
  profile_version: number;
  status: string;
  style_summary: string;
  source_sample_count: number;
  quality_rule_version: number;
  created_by: string;
  created_at?: string | null;
};

export type QualityRule = {
  rule_version: number;
  identity_filters: Record<string, any>;
  text_filters: Record<string, any>;
  template_filters: Record<string, any>;
  scoring_thresholds: Record<string, any>;
  scene_weights: Record<string, any>;
  forbidden_patterns: Record<string, any>;
  updated_by: string;
  updated_at?: string | null;
};

export type QualityRuleForm = {
  exclude_bots: boolean;
  exclude_managed_accounts: boolean;
  min_length: number;
  max_length: number;
  keywords: string[];
  similarity_threshold: number;
  phrases: string[];
  accepted: number;
  downweighted: number;
  group_chat_weight: number;
  channel_comment_weight: number;
  discussion_reply_weight: number;
  forbidden_mode: string;
  forbidden_keywords: string[];
  links: boolean;
  contacts: boolean;
};

export const TASK_LABELS: Record<string, string> = {
  group_ai_chat: 'AI 活群',
  channel_comment: '频道评论',
  discussion_reply: '回复',
};

export function formatDateTime(value?: string | null) {
  return formatBeijingDateTime(value);
}

export function errorMessage(error: unknown) {
  if (error instanceof ApiError) return error.message;
  return error instanceof Error ? error.message : String(error);
}

export function ruleToForm(rule: QualityRule | null): QualityRuleForm {
  return {
    exclude_bots: Boolean(rule?.identity_filters?.exclude_bots ?? true),
    exclude_managed_accounts: Boolean(rule?.identity_filters?.exclude_managed_accounts ?? true),
    min_length: Number(rule?.text_filters?.min_length ?? 2),
    max_length: Number(rule?.text_filters?.max_length ?? 4000),
    keywords: Array.isArray(rule?.text_filters?.keywords) ? rule.text_filters.keywords : [],
    similarity_threshold: Number(rule?.template_filters?.similarity_threshold ?? 0.92),
    phrases: Array.isArray(rule?.template_filters?.phrases) ? rule.template_filters.phrases : [],
    accepted: Number(rule?.scoring_thresholds?.accepted ?? 80),
    downweighted: Number(rule?.scoring_thresholds?.downweighted ?? 40),
    group_chat_weight: Number(rule?.scene_weights?.group_chat ?? 1),
    channel_comment_weight: Number(rule?.scene_weights?.channel_comment ?? 1),
    discussion_reply_weight: Number(rule?.scene_weights?.discussion_reply ?? 1),
    forbidden_mode: String(rule?.forbidden_patterns?.mode || 'reject'),
    forbidden_keywords: Array.isArray(rule?.forbidden_patterns?.keywords) ? rule.forbidden_patterns.keywords : [],
    links: Boolean(rule?.forbidden_patterns?.links ?? true),
    contacts: Boolean(rule?.forbidden_patterns?.contacts ?? true),
  };
}

export function formToRule(values: QualityRuleForm, reason: string) {
  return {
    reason,
    identity_filters: {
      exclude_bots: values.exclude_bots,
      exclude_managed_accounts: values.exclude_managed_accounts,
    },
    text_filters: {
      min_length: values.min_length,
      max_length: values.max_length,
      keywords: values.keywords ?? [],
    },
    template_filters: {
      similarity_threshold: values.similarity_threshold,
      phrases: values.phrases ?? [],
    },
    scoring_thresholds: {
      accepted: values.accepted,
      downweighted: values.downweighted,
    },
    scene_weights: {
      group_chat: values.group_chat_weight,
      channel_comment: values.channel_comment_weight,
      discussion_reply: values.discussion_reply_weight,
    },
    forbidden_patterns: {
      mode: values.forbidden_mode,
      keywords: values.forbidden_keywords ?? [],
      links: values.links,
      contacts: values.contacts,
    },
  };
}

export function candidateKey(item: SourceCandidate) {
  return item.source_key || String(item.target_id ?? item.group_id ?? item.tg_peer_id);
}

export function selectedSourceKeys(sources: LearningSource[], candidates: SourceCandidate[]): Key[] {
  const activeTargetIds = new Set(sources.filter((item) => item.is_enabled).map((item) => item.target_id));
  const activeKeys = candidates.filter((item) => item.target_id && activeTargetIds.has(item.target_id)).map(candidateKey);
  return activeKeys;
}
