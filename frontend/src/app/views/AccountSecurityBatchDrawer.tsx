import React from 'react';
import { Alert, Button, Divider, Drawer, Input, InputNumber, Select, Space, Steps, Switch, Table, Tag, Typography, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { Activity, CheckCircle2, RefreshCcw } from 'lucide-react';
import { api } from '../../shared/api/client';
import type { Account, AccountSecurityBatch, AccountSecurityBatchItem, AccountSecurityPrecheck, AccountSecurityPreviewItem } from '../types';
import { StatusBadge } from '../components/shared';

type Mode = 'cleanup_devices' | 'set_two_fa' | 'profile';

type ProfileStrategy = {
  generation_mode: string;
  language_style: string;
  persona_style: string;
  gender_bias: string;
  age_style: string;
  bio_enabled: boolean;
  username_enabled: boolean;
  username_prefix_hint: string;
  username_max_attempts: number;
  forbidden_words: string[];
  custom_prompt: string;
  overwrite_existing: boolean;
};

type AvatarStrategy = {
  mode: string;
  material_group_id: number | null;
  avatar_sources: string[];
};

const ACTION_LABEL: Record<string, string> = {
  cleanup_devices: '清理外部设备',
  set_two_fa: '设置二步验证',
  update_profile: '资料姓名简介',
  update_username: '设置 @username',
  update_avatar: '设置头像',
};

const MODE_CONFIG: Record<Mode, { title: string; alertType: 'info' | 'warning'; description: string; actions: string[]; reason: string }> = {
  profile: {
    title: '批量资料初始化',
    alertType: 'info',
    description: '只处理头像、昵称、简介和 username。必须先生成预览，再确认创建批次。',
    actions: ['update_profile', 'update_username', 'update_avatar'],
    reason: '批量资料初始化',
  },
  set_two_fa: {
    title: '批量设置二步密码',
    alertType: 'warning',
    description: '只为未设置二步验证的账号生成并托管二步密码，不会修改资料或清理登录设备。',
    actions: ['set_two_fa'],
    reason: '批量设置二步密码',
  },
  cleanup_devices: {
    title: '批量清理登录设备',
    alertType: 'warning',
    description: '只清理非本平台可信登录设备，保留当前平台 Session，不会修改资料或设置二步密码。',
    actions: ['cleanup_devices'],
    reason: '批量清理登录设备',
  },
};

const defaultProfileStrategy: ProfileStrategy = {
  generation_mode: 'ai_random',
  language_style: '中文',
  persona_style: '自然用户',
  gender_bias: '不限',
  age_style: '不限',
  bio_enabled: true,
  username_enabled: true,
  username_prefix_hint: '',
  username_max_attempts: 3,
  forbidden_words: [],
  custom_prompt: '像真实 TG 普通用户的随手昵称，不要正式姓名；可以像锅巴洋芋、蕉太狼、早睡失败、小熊便利店、不吃香菜这种随机网名。',
  overwrite_existing: false,
};

const defaultAvatarStrategy: AvatarStrategy = {
  mode: 'none',
  material_group_id: null,
  avatar_sources: [],
};

function statusText(value: string) {
  const map: Record<string, string> = {
    executable: '可执行',
    manual_required: '需人工处理',
    skipped: '跳过',
    pending: '待执行',
    running: '执行中',
    succeeded: '成功',
    partial_success: '部分成功',
    failed: '失败',
    not_requested: '未请求',
  };
  return map[value] ?? value;
}

function selectedAccounts(accounts: Account[], ids: number[]) {
  const idSet = new Set(ids);
  return accounts.filter((account) => idSet.has(account.id));
}

export function AccountSecurityBatchDrawer({
  open,
  mode,
  accounts,
  selectedAccountIds,
  onClose,
}: {
  open: boolean;
  mode: Mode;
  accounts: Account[];
  selectedAccountIds: number[];
  onClose: () => void;
}) {
  const [actions, setActions] = React.useState<string[]>([]);
  const [profileStrategy, setProfileStrategy] = React.useState<ProfileStrategy>(defaultProfileStrategy);
  const [avatarStrategy, setAvatarStrategy] = React.useState<AvatarStrategy>(defaultAvatarStrategy);
  const [reason, setReason] = React.useState('');
  const [confirmText, setConfirmText] = React.useState('');
  const [precheck, setPrecheck] = React.useState<AccountSecurityPrecheck | null>(null);
  const [batch, setBatch] = React.useState<AccountSecurityBatch | null>(null);
  const [editedPreviewIds, setEditedPreviewIds] = React.useState<Set<number>>(new Set());
  const [loading, setLoading] = React.useState(false);
  const [step, setStep] = React.useState(0);
  const selected = React.useMemo(() => selectedAccounts(accounts, selectedAccountIds), [accounts, selectedAccountIds]);
  const forbiddenText = profileStrategy.forbidden_words.join('，');
  const modeConfig = MODE_CONFIG[mode];
  const isProfileMode = mode === 'profile';

  React.useEffect(() => {
    if (!open) return;
    setActions(modeConfig.actions);
    setProfileStrategy(defaultProfileStrategy);
    setAvatarStrategy(defaultAvatarStrategy);
    setReason(modeConfig.reason);
    setConfirmText('');
    setPrecheck(null);
    setBatch(null);
    setEditedPreviewIds(new Set());
    setStep(0);
  }, [modeConfig, open]);

  const previewOverrides = React.useMemo(() => (precheck?.items ?? []).filter((item) => editedPreviewIds.has(item.account_id)).map((item) => ({
    account_id: item.account_id,
    generated_display_name: item.generated_display_name,
    generated_first_name: item.generated_first_name,
    generated_last_name: item.generated_last_name,
    generated_bio: item.generated_bio,
    username_candidates: item.username_candidates,
    avatar_source: item.avatar_source,
  })), [editedPreviewIds, precheck]);

  const payload = {
    account_ids: selectedAccountIds,
    action_types: actions,
    password_strategy: 'system_unique_encrypted',
    profile_strategy: profileStrategy,
    avatar_strategy: avatarStrategy,
    preview_overrides: [] as typeof previewOverrides,
    recovery_email: '',
    reason,
  };

  function updatePreviewItem(accountId: number, patch: Partial<AccountSecurityPreviewItem>) {
    setPrecheck((current) => {
      if (!current) return current;
      return {
        ...current,
        items: current.items.map((item) => (item.account_id === accountId ? { ...item, ...patch } : item)),
      };
    });
    setEditedPreviewIds((current) => new Set(current).add(accountId));
    setBatch(null);
  }

  async function runPrecheck() {
    if (!selectedAccountIds.length) {
      void message.warning('请先选择账号');
      return;
    }
    setLoading(true);
    try {
      const endpoint = isProfileMode ? '/tg-accounts/security-batches/profile-preview' : '/tg-accounts/security-batches/precheck';
      const timeoutMs = isProfileMode ? Math.min(210_000, Math.max(60_000, selectedAccountIds.length * 5_000)) : 60_000;
      const result = await api<AccountSecurityPrecheck>(endpoint, { method: 'POST', body: JSON.stringify(payload), timeoutMs });
      setPrecheck(result);
      setEditedPreviewIds(new Set());
      setBatch(null);
      setStep(1);
      void message.success('预检完成');
    } finally {
      setLoading(false);
    }
  }

  async function createBatch() {
    if (!precheck) {
      await runPrecheck();
      return;
    }
    if (confirmText !== '确认加固') {
      void message.warning('请输入确认加固');
      return;
    }
    setLoading(true);
    try {
      const result = await api<AccountSecurityBatch>('/tg-accounts/security-batches', {
        method: 'POST',
        body: JSON.stringify({ ...payload, preview_overrides: previewOverrides, confirm_text: confirmText }),
      });
      setBatch(result);
      setStep(2);
      void message.success(`批次 #${result.id} 已创建`);
    } finally {
      setLoading(false);
    }
  }

  const previewColumns: ColumnsType<AccountSecurityPreviewItem> = [
    {
      title: '账号',
      dataIndex: 'account_name',
      width: 180,
      fixed: 'left',
      render: (_, item) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{item.account_name}</Typography.Text>
          <Typography.Text type="secondary">{item.phone_number || item.phone_masked}</Typography.Text>
        </Space>
      ),
    },
    {
      title: '安全状态',
      width: 180,
      render: (_, item) => (
        <Space direction="vertical" size={2}>
          <StatusBadge status={item.trusted_session_status} label={`可信设备 ${item.trusted_session_status}`} />
          <Typography.Text type="secondary">外部设备：{item.external_authorization_count}</Typography.Text>
          <Typography.Text type="secondary">2FA：{item.two_fa_status}</Typography.Text>
        </Space>
      ),
    },
    {
      title: '资料预览（可编辑）',
      width: 320,
      render: (_, item) => (
        <Space direction="vertical" size={6} style={{ width: '100%' }}>
          <Input
            size="small"
            value={item.generated_display_name}
            placeholder="昵称 / 展示名"
            onChange={(event) => updatePreviewItem(item.account_id, { generated_display_name: event.target.value })}
          />
          <Space.Compact style={{ width: '100%' }}>
            <Input
              size="small"
              value={item.generated_last_name}
              placeholder="姓 / last_name"
              onChange={(event) => updatePreviewItem(item.account_id, { generated_last_name: event.target.value })}
            />
            <Input
              size="small"
              value={item.generated_first_name}
              placeholder="名 / first_name"
              onChange={(event) => updatePreviewItem(item.account_id, { generated_first_name: event.target.value })}
            />
          </Space.Compact>
          <Input
            size="small"
            value={item.generated_bio}
            placeholder="简介 bio"
            onChange={(event) => updatePreviewItem(item.account_id, { generated_bio: event.target.value })}
          />
          <Input
            size="small"
            value={item.username_candidates.join(',')}
            placeholder="username 候选，用逗号分隔"
            onChange={(event) => updatePreviewItem(item.account_id, { username_candidates: event.target.value.split(/[,，\s]+/).map((value) => value.trim().replace(/^@/, '')).filter(Boolean) })}
          />
          <Input
            size="small"
            value={item.avatar_source}
            placeholder="头像来源"
            onChange={(event) => updatePreviewItem(item.account_id, { avatar_source: event.target.value })}
          />
        </Space>
      ),
    },
    {
      title: '校验',
      width: 220,
      render: (_, item) => (
        <Space direction="vertical" size={4}>
          <StatusBadge status={item.precheck_status} label={statusText(item.precheck_status)} />
          {item.blockers.map((blocker) => <Tag color="red" key={blocker}>{blocker}</Tag>)}
          {item.warnings.map((warning) => <Tag color="gold" key={warning}>{warning}</Tag>)}
        </Space>
      ),
    },
  ];

  const itemColumns: ColumnsType<AccountSecurityBatchItem> = [
    { title: '账号ID', dataIndex: 'account_id', width: 90, fixed: 'left' },
    { title: '总状态', dataIndex: 'status', width: 120, render: (value) => <StatusBadge status={value} label={statusText(value)} /> },
    { title: '设备', dataIndex: 'cleanup_status', width: 120, render: (value) => statusText(value) },
    { title: '2FA', dataIndex: 'two_fa_status', width: 120, render: (value) => statusText(value) },
    { title: '资料', dataIndex: 'profile_status', width: 120, render: (value) => statusText(value) },
    { title: 'username', dataIndex: 'username_status', width: 140, render: (_, item) => item.generated_username || statusText(item.username_status) },
    { title: '失败原因', dataIndex: 'failure_detail', width: 260, render: (value, item) => value || item.skipped_reason || '-' },
  ];

  return (
    <Drawer
      title={modeConfig.title}
      width={1120}
      open={open}
      destroyOnClose
      onClose={onClose}
      extra={<Button onClick={onClose}>关闭</Button>}
    >
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        <Alert
          type={modeConfig.alertType}
          showIcon
          message={`已选择 ${selected.length} 个账号`}
          description={modeConfig.description}
        />
        <Steps
          current={step}
          items={[
            { title: '选择动作' },
            { title: '预检预览' },
            { title: '批次结果' },
          ]}
        />
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Typography.Text strong>动作范围</Typography.Text>
          <Space wrap>
            {actions.map((value) => <Tag color="processing" key={value}>{ACTION_LABEL[value]}</Tag>)}
          </Space>
        </Space>
        {isProfileMode && (
          <>
            <Divider />
            <Space direction="vertical" size={12} style={{ width: '100%' }}>
              <Typography.Text strong><Activity size={16} /> AI 随机命名与资料策略</Typography.Text>
              <Space wrap>
                <Select
                  value={profileStrategy.generation_mode}
                  style={{ width: 150 }}
                  options={[
                    { label: 'AI 随机生成', value: 'ai_random' },
                    { label: '模板兜底', value: 'template' },
                    { label: '手工序号', value: 'sequence' },
                  ]}
                  onChange={(generation_mode) => setProfileStrategy((current) => ({ ...current, generation_mode }))}
                />
                <Select value={profileStrategy.language_style} style={{ width: 120 }} options={['中文', '英文', '混合', '东南亚'].map((value) => ({ label: value, value }))} onChange={(language_style) => setProfileStrategy((current) => ({ ...current, language_style }))} />
                <Select value={profileStrategy.persona_style} style={{ width: 140 }} options={['自然用户', '行业用户', '客服', '社区成员'].map((value) => ({ label: value, value }))} onChange={(persona_style) => setProfileStrategy((current) => ({ ...current, persona_style }))} />
                <Select value={profileStrategy.gender_bias} style={{ width: 110 }} options={['不限', '偏男性', '偏女性', '中性'].map((value) => ({ label: value, value }))} onChange={(gender_bias) => setProfileStrategy((current) => ({ ...current, gender_bias }))} />
                <InputNumber min={1} max={10} value={profileStrategy.username_max_attempts} addonBefore="候选数" onChange={(username_max_attempts) => setProfileStrategy((current) => ({ ...current, username_max_attempts: username_max_attempts ?? 3 }))} />
                <Input
                  style={{ width: 180 }}
                  value={profileStrategy.username_prefix_hint}
                  placeholder="username 前缀"
                  onChange={(event) => setProfileStrategy((current) => ({ ...current, username_prefix_hint: event.target.value }))}
                />
              </Space>
              <Space wrap>
                <Switch checked={profileStrategy.bio_enabled} onChange={(bio_enabled) => setProfileStrategy((current) => ({ ...current, bio_enabled }))} /> <span>生成简介</span>
                <Switch checked={profileStrategy.username_enabled} onChange={(username_enabled) => setProfileStrategy((current) => ({ ...current, username_enabled }))} /> <span>生成 username 候选</span>
                <Switch checked={profileStrategy.overwrite_existing} onChange={(overwrite_existing) => setProfileStrategy((current) => ({ ...current, overwrite_existing }))} /> <span>覆盖已有资料</span>
                <Select
                  value={avatarStrategy.mode}
                  style={{ width: 160 }}
                  options={[
                    { label: '不改头像', value: 'none' },
                    { label: '随机素材池', value: 'material_random' },
                    { label: '顺序分配', value: 'sequential' },
                  ]}
                  onChange={(modeValue) => setAvatarStrategy((current) => ({ ...current, mode: modeValue }))}
                />
                <Input
                  style={{ width: 260 }}
                  value={forbiddenText}
                  placeholder="禁用词，用逗号分隔"
                  onChange={(event) => setProfileStrategy((current) => ({ ...current, forbidden_words: event.target.value.split(/[,，\s]+/).filter(Boolean) }))}
                />
              </Space>
              <Input.TextArea
                rows={2}
                value={profileStrategy.custom_prompt}
                placeholder="命名风格提示：例如 像锅巴洋芋、蕉太狼、早睡失败这种真实 TG 昵称，不要正式姓名"
                onChange={(event) => setProfileStrategy((current) => ({ ...current, custom_prompt: event.target.value }))}
              />
              <Input.TextArea
                rows={2}
                value={avatarStrategy.avatar_sources.join('\n')}
                placeholder="头像来源：每行一个 avatar:对象key / material:素材ID / 平台媒体文件路径"
                onChange={(event) => setAvatarStrategy((current) => ({ ...current, avatar_sources: event.target.value.split(/\n+/).map((item) => item.trim()).filter(Boolean) }))}
              />
            </Space>
          </>
        )}
        <Input.TextArea rows={2} value={reason} placeholder="操作原因" onChange={(event) => setReason(event.target.value)} />
        <Space wrap>
          <Button icon={<RefreshCcw size={16} />} loading={loading} onClick={runPrecheck}>预检 / AI 生成预览</Button>
          <Button icon={<Activity size={16} />} loading={loading} onClick={runPrecheck}>重抽全部</Button>
          <Input value={confirmText} style={{ width: 160 }} placeholder="输入：确认加固" onChange={(event) => setConfirmText(event.target.value)} />
          <Button
            type="primary"
            icon={<CheckCircle2 size={16} />}
            disabled={!precheck || confirmText !== '确认加固' || ((precheck.summary.executable ?? 0) < 1 && editedPreviewIds.size < 1)}
            loading={loading}
            onClick={createBatch}
          >
            确认创建批次
          </Button>
        </Space>
        {precheck && (
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <Typography.Text strong>预检汇总：共 {precheck.summary.total ?? 0} 个，可执行 {precheck.summary.executable ?? 0} 个，需等待 {precheck.summary.waiting ?? 0} 个，需人工处理 {precheck.summary.manual_required ?? 0} 个</Typography.Text>
            <Table<AccountSecurityPreviewItem>
              className="tg-table"
              rowKey="account_id"
              columns={previewColumns}
              dataSource={precheck.items}
              pagination={{ pageSize: 6 }}
              scroll={{ x: 920 }}
            />
          </Space>
        )}
        {batch && (
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <Typography.Text strong>批次 #{batch.id}：{statusText(batch.status)}，成功 {batch.success_count}，失败 {batch.failed_count}，跳过 {batch.skipped_count}</Typography.Text>
            <Typography.Text type="secondary">trace_id：{batch.trace_id}</Typography.Text>
            <Table<AccountSecurityBatchItem>
              className="tg-table"
              rowKey="id"
              columns={itemColumns}
              dataSource={batch.items}
              pagination={{ pageSize: 6 }}
              scroll={{ x: 960 }}
            />
          </Space>
        )}
      </Space>
    </Drawer>
  );
}
