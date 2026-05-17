import type { AccountLoginForm, AdminUserForm, AuditFilters } from '../types';

export const EMPTY_ACCOUNT_LOGIN_FORM: AccountLoginForm = {
  account: null,
  step: 'method',
  method: 'code',
  code: '',
  password_2fa: '',
  flow: null,
  error: '',
};

export function defaultAdminUserForm(): AdminUserForm {
  return {
    id: null,
    name: '',
    password: '',
    role: '普通用户',
    role_template: '账号添加专员',
    subscription_status: 'pending_activation',
    menu_permissions: ['overview.view', 'accounts.view', 'accounts.create', 'accounts.login', 'accounts.sync'],
    permissions: ['overview.view', 'accounts.view', 'accounts.create', 'accounts.login', 'accounts.sync'],
    is_active: true,
  };
}

export function defaultAuditFilters(): AuditFilters {
  return { actor: '', action: '', target_type: '', target_id: '', keyword: '', account_id: '', operation_target_id: '', task_id: '', status: '', start_at: '', end_at: '' };
}

export function defaultAccountCreateForm() {
  return {
    display_name: '新托管账号',
    username: '',
    phone_number: '',
    pool_id: '' as number | '',
    login_method: 'code' as 'code' | 'qr',
  };
}

export function defaultAccountPoolForm() {
  return {
    name: '新账号分组',
    description: '',
    is_default: false,
  };
}

export function defaultCloneForm() {
  return {
    target_account_ids: [] as number[],
    clone_contacts: true,
    clone_groups: true,
  };
}

export function defaultProfileForm() {
  return {
    display_name: '',
    tg_first_name: '',
    tg_last_name: '',
    tg_bio: '',
    avatar_object_key: '',
  };
}

export function defaultGroupPolicy() {
  return {
    active_window: '09:00-23:00',
    daily_limit: 120,
    account_cooldown_seconds: 180,
    group_cooldown_seconds: 60,
    topic_direction: '',
    banned_words: '',
    link_whitelist: '',
    require_review: false,
    listener_enabled: false,
    listener_auto_reply_enabled: true,
    listener_interval_seconds: 60,
    listener_context_limit: 20,
    listener_account_ids: [] as number[],
  };
}

export function defaultDeveloperAppForm() {
  return {
    id: null as number | null,
    app_name: 'Telegram 开发者应用',
    api_id: '',
    api_hash: '',
    max_accounts: 0,
    notes: '',
    is_active: true,
  };
}

export function defaultTenantForm() {
  return {
    id: null as number | null,
    name: '',
    plan_name: '',
    account_quota: 50,
    task_quota: 5000,
  };
}

export function defaultAiProviderForm() {
  return {
    id: null as number | null,
    provider_name: 'DeepSeek',
    base_url: 'https://api.deepseek.com',
    model_name: 'deepseek-v4-flash',
    api_key: '',
    api_key_header: 'Authorization',
    notes: '',
    is_active: true,
  };
}

export function defaultPromptTemplateForm() {
  return {
    id: null as number | null,
    name: '运营群活跃模板',
    template_type: '群活跃对话计划',
    content: '请为 {{group_title}} 围绕 {{topic}} 生成 {{count}} 条自然 Telegram 群聊发言计划，语气 {{tone}}，素材 {{materials}}，输出 JSON turns，并包含角色、意图、延迟和自动校验建议。',
    is_active: true,
  };
}

export function defaultMaterialForm() {
  return {
    id: null as number | null,
    title: '活动表情包',
    material_type: '表情包',
    content: 'https://example.local/stickers/welcome.webp',
    tags: '表情包,欢迎',
    emoji_asset_kind: 'image_meme',
    cache_ready_status: 'not_cached',
    delivery_mode: 'download_reupload',
    source_kind: 'url',
  };
}

export function defaultKeywordRuleForm() {
  return {
    id: null as number | null,
    keyword: '',
    match_type: 'contains',
    is_active: true,
    note: '',
  };
}

export function defaultDirectMessageForm() {
  return {
    target_peer_id: '',
    target_display: '',
    content: '',
  };
}
