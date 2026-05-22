export type Contact = {
  id: number;
  account_id: number;
  peer_id: string;
  display_name: string;
  username: string | null;
  phone_masked: string;
  phone_number?: string | null;
  contact_type: string;
  is_mutual: boolean;
  last_message_at: string | null;
  last_synced_at: string;
};

export type Group = {
  id: number;
  tg_peer_id: string;
  title: string;
  group_type: string;
  member_count: number;
  auth_status: string;
  can_send: boolean;
  active_window: string;
  daily_limit: number;
  account_cooldown_seconds: number;
  group_cooldown_seconds: number;
  topic_direction: string;
  banned_words: string;
  link_whitelist: string;
  require_review: boolean;
  listener_enabled: boolean;
  listener_auto_reply_enabled: boolean;
  listener_interval_seconds: number;
  listener_context_limit: number;
  listener_last_polled_at: string | null;
  listener_last_reply_at: string | null;
  listener_last_error: string;
  listener_account_ids: number[];
};

export type MessageTask = {
  id: number;
  campaign_id: number | null;
  group_id: number | null;
  account_id: number | null;
  draft_id: number | null;
  content: string;
  message_type: string;
  material_id: number | null;
  target_type: string;
  target_peer_id: string | null;
  target_display: string;
  preferred_account_id: number | null;
  preferred_account_name?: string | null;
  actual_account_changed: boolean;
  planned_delay_seconds: number;
  scheduled_at: string;
  status: string;
  failure_type: string | null;
  failure_detail: string | null;
  media_sent: boolean | null;
  media_failure_reason: string;
  sent_at: string | null;
};

export type MessageType = '文本' | '图片' | '表情包' | '文件' | '链接' | '组合消息';

export type MessageSendTaskCreate = {
  account_id: number;
  target_type: 'private' | 'group' | 'channel';
  target_peer_id?: string | null;
  target_display?: string;
  group_id?: number | null;
  operation_target_id?: number | null;
  content: string;
  message_type: MessageType;
  material_id?: number | null;
  jitter_min_seconds: number;
  jitter_max_seconds: number;
  dispatch_now: boolean;
  scheduled_at?: string | null;
};

export type MessageSendTarget = {
  target_type: 'private' | 'group' | 'channel';
  target_peer_id?: string | null;
  target_display?: string;
  group_id?: number | null;
  operation_target_id?: number | null;
};

export type MessageSendBatchCreate = {
  account_id: number;
  targets: MessageSendTarget[];
  content: string;
  message_type: MessageType;
  material_id?: number | null;
  dispatch_now: boolean;
  scheduled_at?: string | null;
};
