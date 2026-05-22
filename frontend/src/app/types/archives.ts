export type ArchiveItem = {
  id: number;
  group_id: number;
  collection_account_id?: number | null;
  title: string;
  status: string;
  sync_mode: string;
  failure_detail: string;
  message_count: number;
  member_count: number;
  summary: string;
  new_group_plan: string;
  started_at?: string | null;
  finished_at?: string | null;
  last_synced_at?: string | null;
};

export type ArchiveDetail = {
  archive: ArchiveItem;
  messages: Array<{ id: number; sender_name: string; sender_peer_id?: string; remote_message_id?: string; sender_phone_masked?: string; sender_phone_number?: string | null; content: string; message_type: string; sent_at: string }>;
  members: Array<{ id: number; display_name: string; username: string | null; peer_id?: string; phone_masked?: string; phone_number?: string | null; activity_score: number; tags: string; last_seen_at?: string | null }>;
  invite_candidates: Array<{ id: number; display_name: string; username: string | null; peer_id?: string; phone_masked?: string; phone_number?: string | null; activity_score: number; tags: string; last_seen_at?: string | null }>;
};

export type ArchiveExport = ArchiveDetail & {
  export_format: string;
  generated_at: string;
  message_count: number;
  member_count: number;
};

export type AuditLog = {
  id: number;
  tenant_id: number | null;
  actor: string;
  action: string;
  target_type: string;
  target_id: string;
  detail: string;
  account_display_name?: string | null;
  account_phone_number?: string | null;
  ip_address: string;
  created_at: string;
};

export type AuditFilters = {
  actor: string;
  action: string;
  target_type: string;
  target_id: string;
  keyword: string;
  account_id: string;
  operation_target_id: string;
  task_id: string;
  status: string;
  start_at: string;
  end_at: string;
};
