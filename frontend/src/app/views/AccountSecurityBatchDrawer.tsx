import React from 'react';
import { Alert, Button, Divider, Drawer, Input, InputNumber, Modal, Select, Space, Steps, Switch, Table, Tag, Typography, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { Activity, CheckCircle2, RefreshCcw } from 'lucide-react';
import { api } from '../../shared/api/client';
import type { Account, AccountSecurityBatch, AccountSecurityBatchItem, AccountSecurityPrecheck, AccountSecurityPreviewItem } from '../types';
import { StatusBadge } from '../components/shared';

type Mode = 'cleanup_devices' | 'set_two_fa' | 'profile' | 'standby_session';

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
  provision_standby_session: '补齐备用 session',
  self_heal_session: '自愈恢复 session',
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
    description: '只保留当前 session、已确认 hash 的 primary / standby_1 / standby_2 和一个官方锚点设备，不会修改资料或设置二步密码。',
    actions: ['cleanup_devices'],
    reason: '批量清理登录设备',
  },
  standby_session: {
    title: '批量补齐备用 session',
    alertType: 'warning',
    description: '按账号授权槽位补齐 standby_1 / standby_2，使用验证码读取能力和平台托管 2FA；失败原因会进入任务中心。',
    actions: ['provision_standby_session', 'self_heal_session'],
    reason: '批量补齐备用 session',
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
  custom_prompt: '像真实 TG 普通用户的随手昵称，不要正式姓名；整批要明显不一样，昵称长短、简介字数、username 前缀都不要同一模板；可以像锅巴洋芋、蕉太狼、早睡失败、小熊便利店、不吃香菜这种随机网名。',
  overwrite_existing: false,
};

const defaultAvatarStrategy: AvatarStrategy = {
  mode: 'none',
  material_group_id: null,
  avatar_sources: [],
};
const BATCH_SELECTION_LIMIT = 100;

function statusText(value: string) {
  const map: Record<string, string> = {
    executable: '可执行',
    manual_required: '需人工处理',
    skipped: '跳过',
    pending: '待执行',
    code_waiting: '等待验证码',
    two_fa_waiting: '等待 2FA',
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

function accountNeedsProfile(account: Account) {
  return !account.avatar_object_key || !account.username || !account.tg_first_name || account.profile_sync_status === '待处理';
}

function accountMatchesBatchFilter(account: Account, filter: string) {
  if (filter === 'active') return account.status === '在线';
  if (filter === 'login_required') return ['待登录', '等待验证码', '等待扫码', '等待2FA', '需重新登录', '异常'].includes(account.status);
  if (filter === 'profile_incomplete') return accountNeedsProfile(account);
  if (filter === 'proxy_alert') return Boolean(account.proxy_alert_status || account.proxy_status === '异常');
  if (filter === 'standby_gap') return account.authorization_summary.standby_count < account.authorization_summary.target_standby_count;
  if (filter === 'standby_recoverable') return account.authorization_summary.primary_status !== 'active' && account.authorization_summary.standby_count > 0;
  if (filter === 'device_cleanup_missing') return account.authorization_summary.risk_hint.includes('设备');
  return true;
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
  const [confirmOpen, setConfirmOpen] = React.useState(false);
  const [precheck, setPrecheck] = React.useState<AccountSecurityPrecheck | null>(null);
  const [precheckPayloadSignature, setPrecheckPayloadSignature] = React.useState('');
  const [batch, setBatch] = React.useState<AccountSecurityBatch | null>(null);
  const [editedPreviewIds, setEditedPreviewIds] = React.useState<Set<number>>(new Set());
  const [draftAccountIds, setDraftAccountIds] = React.useState<number[]>([]);
  const [accountQuery, setAccountQuery] = React.useState('');
  const [accountFilter, setAccountFilter] = React.useState('all');
  const [rangeStart, setRangeStart] = React.useState(1);
  const [rangeEnd, setRangeEnd] = React.useState(BATCH_SELECTION_LIMIT);
  const [standbySlotStrategy, setStandbySlotStrategy] = React.useState('auto_missing');
  const [loading, setLoading] = React.useState(false);
  const [step, setStep] = React.useState(0);
  const selected = React.useMemo(() => selectedAccounts(accounts, draftAccountIds), [accounts, draftAccountIds]);
  const profileIncompleteAccounts = React.useMemo(() => accounts.filter(accountNeedsProfile), [accounts]);
  const filteredAccounts = React.useMemo(() => {
    const query = accountQuery.trim().toLowerCase();
    return accounts.filter((account) => {
      if (!accountMatchesBatchFilter(account, accountFilter)) return false;
      if (!query) return true;
      const haystack = [
        account.display_name,
        account.username,
        account.phone_masked,
        account.phone_number,
        account.pool_name,
        account.status,
        account.profile_sync_status,
        account.proxy_name,
        account.proxy_status,
        account.authorization_summary?.standby_count < account.authorization_summary?.target_standby_count ? '备用 session 缺口 健康备用 session 不足 2 个 standby_1 session 缺失 standby_2 session 缺失 备用 session 未登录' : '',
        account.authorization_summary?.primary_status !== 'active' && account.authorization_summary?.standby_count > 0 ? '可从备用 session 激活恢复' : '',
        account.authorization_summary?.risk_hint?.includes('设备') ? '未做过登录设备清理 外部设备未清理 最近设备清理失败' : '',
      ].filter(Boolean).join(' ').toLowerCase();
      return haystack.includes(query);
    });
  }, [accountFilter, accountQuery, accounts]);
  const forbiddenText = profileStrategy.forbidden_words.join('，');
  const modeConfig = MODE_CONFIG[mode];
  const isProfileMode = mode === 'profile';
  const precheckButtonLabel = mode === 'standby_session' ? '预检备用 session 补齐' : isProfileMode ? '预检 / AI 生成预览' : '预检动作';
  const confirmButtonLabel = mode === 'standby_session' ? '确认补齐备用 session' : isProfileMode ? '确认执行资料初始化' : '确认创建批次';
  const batchResultTargetLabel = mode === 'standby_session' ? '备用 session 状态' : isProfileMode ? '资料变化' : '安全状态';
  const standbyNoExecutable = mode === 'standby_session' && precheck && (precheck.summary.executable ?? 0) < 1;
  const autoSkippedCount = (precheck?.summary.skipped ?? 0) + (precheck?.summary.manual_required ?? 0);
  const avatarSourceHint = avatarStrategy.mode === 'random_from_material_pool'
    ? '系统会从素材中心已审核的头像包 / 上传图片中随机分配头像，不需要填写 material ID 或路径。'
    : avatarStrategy.mode === 'sequential'
      ? '留空则按素材中心头像包顺序分配；填写来源时按你填写的顺序覆盖。'
      : '';
  const shouldShowAvatarSourceInput = avatarStrategy.mode === 'sequential';

  React.useEffect(() => {
    if (!open) return;
    setActions(modeConfig.actions);
    setProfileStrategy(defaultProfileStrategy);
    setAvatarStrategy(defaultAvatarStrategy);
    setReason(modeConfig.reason);
    setConfirmOpen(false);
    setPrecheck(null);
    setPrecheckPayloadSignature('');
    setBatch(null);
    setEditedPreviewIds(new Set());
    setDraftAccountIds(selectedAccountIds);
    setAccountQuery('');
    setAccountFilter('all');
    setRangeStart(1);
    setRangeEnd(BATCH_SELECTION_LIMIT);
    setStandbySlotStrategy('auto_missing');
    setStep(0);
  }, [modeConfig, open, selectedAccountIds]);

  const previewOverrides = React.useMemo(() => (precheck?.items ?? []).map((item) => ({
    account_id: item.account_id,
    generated_display_name: item.generated_display_name,
    generated_first_name: item.generated_first_name,
    generated_last_name: item.generated_last_name,
    generated_bio: item.generated_bio,
    username_candidates: item.username_candidates,
    avatar_source: item.avatar_source,
  })), [precheck]);

  const payload = {
    account_ids: draftAccountIds,
    action_types: actions,
    standby_slot_strategy: standbySlotStrategy,
    password_strategy: 'system_unique_encrypted',
    profile_strategy: profileStrategy,
    avatar_strategy: avatarStrategy,
    preview_overrides: [] as typeof previewOverrides,
    recovery_email: '',
    reason,
  };
  const payloadSignature = React.useMemo(() => JSON.stringify({
    account_ids: draftAccountIds,
    action_types: actions,
    standby_slot_strategy: standbySlotStrategy,
    profile_strategy: profileStrategy,
    avatar_strategy: avatarStrategy,
    reason,
  }), [actions, avatarStrategy, draftAccountIds, profileStrategy, reason, standbySlotStrategy]);
  const batchDrawerRequestRef = React.useRef({ kind: '', signature: '', seq: 0 });
  const latestBatchPayloadSignatureRef = React.useRef(payloadSignature);

  React.useEffect(() => {
    latestBatchPayloadSignatureRef.current = payloadSignature;
  }, [payloadSignature]);

  function beginBatchDrawerRequest(kind: string, signature: string) {
    const nextSeq = batchDrawerRequestRef.current.seq + 1;
    batchDrawerRequestRef.current = { kind, signature, seq: nextSeq };
    return nextSeq;
  }

  function isActiveBatchDrawerRequest(kind: string, signature: string, requestSeq: number) {
    return batchDrawerRequestRef.current.kind === kind
      && batchDrawerRequestRef.current.signature === signature
      && batchDrawerRequestRef.current.seq === requestSeq
      && latestBatchPayloadSignatureRef.current === signature;
  }

  function isCurrentBatchDrawerRequest(kind: string, requestSeq: number) {
    return batchDrawerRequestRef.current.kind === kind && batchDrawerRequestRef.current.seq === requestSeq;
  }

  React.useEffect(() => {
    if (!precheck || !precheckPayloadSignature || precheckPayloadSignature === payloadSignature) return;
    setPrecheck(null);
    setBatch(null);
    setConfirmOpen(false);
    setEditedPreviewIds(new Set());
    setStep(0);
  }, [payloadSignature, precheck, precheckPayloadSignature]);

  function errorMessage(error: unknown, fallback: string) {
    return error instanceof Error ? error.message : fallback;
  }

  function mergeDraftAccountIds(ids: number[]) {
    setDraftAccountIds((current) => Array.from(new Set([...current, ...ids])));
  }

  function selectFilteredRange() {
    if (!filteredAccounts.length) {
      void message.warning('当前筛选下没有可选账号');
      return;
    }
    const start = Math.max(1, Math.min(rangeStart, filteredAccounts.length));
    const end = Math.max(start, Math.min(rangeEnd, filteredAccounts.length));
    setRangeStart(start);
    setRangeEnd(end);
    mergeDraftAccountIds(filteredAccounts.slice(start - 1, end).map((account) => account.id));
  }

  function showProfileIncompleteAccounts() {
    setAccountFilter('profile_incomplete');
    setAccountQuery('');
  }

  function selectProfileIncompleteAccounts() {
    if (!profileIncompleteAccounts.length) {
      void message.warning('当前没有资料待初始化账号');
      return;
    }
    mergeDraftAccountIds(profileIncompleteAccounts.map((account) => account.id));
  }

  function showStandbyGapAccounts() {
    setAccountFilter('standby_gap');
    setAccountQuery('');
  }

  function selectStandbyGapAccounts() {
    const standbyGapAccounts = accounts.filter((account) => accountMatchesBatchFilter(account, 'standby_gap'));
    if (!standbyGapAccounts.length) {
      void message.warning('当前没有备用 session 缺口账号');
      return;
    }
    mergeDraftAccountIds(standbyGapAccounts.map((account) => account.id));
  }

  function changeAvatarMode(modeValue: string) {
    setAvatarStrategy((current) => ({
      ...current,
      mode: modeValue,
      avatar_sources: modeValue === 'sequential' ? current.avatar_sources : [],
    }));
  }

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
    if (!draftAccountIds.length) {
      void message.warning('请在当前批量动作中选择账号');
      return;
    }
    const requestSignature = payloadSignature;
    const requestSeq = beginBatchDrawerRequest('precheck', requestSignature);
    setLoading(true);
    try {
      const endpoint = isProfileMode ? '/tg-accounts/security-batches/profile-preview' : '/tg-accounts/security-batches/precheck';
      const timeoutMs = isProfileMode ? Math.min(210_000, Math.max(60_000, draftAccountIds.length * 5_000)) : 60_000;
      const result = await api<AccountSecurityPrecheck>(endpoint, { method: 'POST', body: JSON.stringify(payload), timeoutMs });
      if (!isActiveBatchDrawerRequest('precheck', requestSignature, requestSeq)) return;
      setPrecheck(result);
      setPrecheckPayloadSignature(payloadSignature);
      setEditedPreviewIds(new Set());
      setBatch(null);
      setStep(1);
      void message.success(`预检完成：共 ${result.summary.total ?? 0} 个，可执行 ${result.summary.executable ?? 0} 个`);
    } catch (error) {
      if (!isActiveBatchDrawerRequest('precheck', requestSignature, requestSeq)) return;
      void message.error(errorMessage(error, '预检失败'));
    } finally {
      if (isCurrentBatchDrawerRequest('precheck', requestSeq)) setLoading(false);
    }
  }

  function openCreateConfirm() {
    if (!precheck) {
      void runPrecheck();
      return;
    }
    if (precheckPayloadSignature !== payloadSignature) {
      setPrecheck(null);
      setBatch(null);
      setConfirmOpen(false);
      setEditedPreviewIds(new Set());
      setStep(0);
      void message.warning('账号或策略已变化，请重新预检');
      return;
    }
    if ((precheck.summary.executable ?? 0) < 1 && editedPreviewIds.size < 1) {
      void message.warning('当前没有可创建的账号，请处理阻塞原因后重新预检');
      return;
    }
    setConfirmOpen(true);
  }

  async function createBatch() {
    if (!precheck) return;
    const requestSignature = payloadSignature;
    const requestSeq = beginBatchDrawerRequest('create', requestSignature);
    setLoading(true);
    try {
      const result = await api<AccountSecurityBatch>('/tg-accounts/security-batches', {
        method: 'POST',
        body: JSON.stringify({ ...payload, preview_overrides: previewOverrides, confirm_text: '确认' }),
      });
      if (!isActiveBatchDrawerRequest('create', requestSignature, requestSeq)) return;
      setBatch(result);
      setStep(2);
      setConfirmOpen(false);
      void message.success(`批次 #${result.id} 已提交后台执行：共 ${result.total_count} 个，自动跳过 ${result.skipped_count} 个；后台 worker 完成后再刷新账号列表查看${batchResultTargetLabel}。`);
    } catch (error) {
      if (!isActiveBatchDrawerRequest('create', requestSignature, requestSeq)) return;
      void message.error(errorMessage(error, '创建批次失败'));
    } finally {
      if (isCurrentBatchDrawerRequest('create', requestSeq)) setLoading(false);
    }
  }

  const previewColumns: ColumnsType<AccountSecurityPreviewItem> = [
    {
      title: '账号',
      dataIndex: 'account_name',
      width: 180,
      fixed: 'left',
      render: (_, item) => (
        <Space orientation="vertical" size={0}>
          <Typography.Text strong>{item.account_name}</Typography.Text>
          <Typography.Text type="secondary">{item.phone_number || item.phone_masked}</Typography.Text>
        </Space>
      ),
    },
    {
      title: '安全状态',
      width: 180,
      render: (_, item) => (
        <Space orientation="vertical" size={2}>
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
        <Space orientation="vertical" size={6} style={{ width: '100%' }}>
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
        <Space orientation="vertical" size={4}>
          <StatusBadge status={item.precheck_status} label={statusText(item.precheck_status)} />
          {item.blockers.map((blocker) => <Tag color="red" key={blocker}>{blocker}</Tag>)}
          {item.warnings.map((warning) => <Tag color="gold" key={warning}>{warning}</Tag>)}
        </Space>
      ),
    },
  ];
  const profilePreviewColumns = previewColumns;
  const actionPrecheckColumns = previewColumns.filter((column) => column.title !== '资料预览（可编辑）');
  const precheckColumns = isProfileMode ? profilePreviewColumns : actionPrecheckColumns;

  const itemColumns: ColumnsType<AccountSecurityBatchItem> = [
    { title: '账号ID', dataIndex: 'account_id', width: 90, fixed: 'left' },
    { title: '总状态', dataIndex: 'status', width: 120, render: (value) => <StatusBadge status={value} label={statusText(value)} /> },
    { title: '设备', dataIndex: 'cleanup_status', width: 120, render: (value) => statusText(value) },
    { title: '2FA', dataIndex: 'two_fa_status', width: 120, render: (value) => statusText(value) },
    { title: '备用 session', dataIndex: 'standby_session_status', width: 140, render: (value) => statusText(value || 'not_requested') },
    { title: '资料', dataIndex: 'profile_status', width: 120, render: (value) => statusText(value) },
    { title: 'username', dataIndex: 'username_status', width: 140, render: (_, item) => item.generated_username || statusText(item.username_status) },
    { title: '失败原因', dataIndex: 'failure_detail', width: 260, render: (value, item) => value || item.skipped_reason || '-' },
  ];
  const accountColumns: ColumnsType<Account> = [
    {
      title: mode === 'standby_session' ? '账号（当前资料）' : '账号',
      dataIndex: 'display_name',
      width: 220,
      render: (_, account) => (
        <Space orientation="vertical" size={0}>
          <Typography.Text strong>{account.display_name}</Typography.Text>
          <Typography.Text type="secondary">{account.username ? `@${account.username}` : account.phone_masked}</Typography.Text>
        </Space>
      ),
    },
    { title: '状态', dataIndex: 'status', width: 110, render: (value) => <Tag>{value}</Tag> },
    { title: '分组', dataIndex: 'pool_name', width: 140, render: (value) => value || '未分组' },
    { title: '资料', key: 'profile', width: 120, render: (_, account) => <Tag color={accountNeedsProfile(account) ? 'gold' : 'green'}>{accountNeedsProfile(account) ? '待初始化' : '完整'}</Tag> },
    { title: '代理', key: 'proxy', width: 160, render: (_, account) => account.proxy_name ? `${account.proxy_name} / ${account.proxy_status || '-'}` : '-' },
  ];

  return (
    <Drawer
      title={modeConfig.title}
      size={1120}
      open={open}
      destroyOnHidden
      onClose={onClose}
      extra={<Button onClick={onClose}>关闭</Button>}
    >
      <Space orientation="vertical" size={16} style={{ width: '100%' }}>
        <Alert
          type={modeConfig.alertType}
          showIcon
          title={`本批次已选择 ${selected.length} 个账号`}
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
        <Space orientation="vertical" size={12} style={{ width: '100%' }}>
          <Typography.Text strong>动作范围</Typography.Text>
          <Space wrap>
            {actions.map((value) => <Tag color="processing" key={value}>{ACTION_LABEL[value]}</Tag>)}
          </Space>
        </Space>
        <Space orientation="vertical" size={8} style={{ width: '100%' }}>
          <Typography.Text strong>选择账号</Typography.Text>
          <Space wrap>
            <Input.Search
              allowClear
              value={accountQuery}
              placeholder="搜索账号、手机号、分组、状态"
              style={{ width: 260 }}
              onChange={(event) => setAccountQuery(event.target.value)}
            />
            <Select
              value={accountFilter}
              style={{ width: 160 }}
              options={[
                { label: '全部账号', value: 'all' },
                { label: '在线账号', value: 'active' },
                { label: '需登录处理', value: 'login_required' },
                { label: '资料待初始化', value: 'profile_incomplete' },
                { label: '代理异常', value: 'proxy_alert' },
                { label: 'standby_1 session 缺失 / standby_2 session 缺失', value: 'standby_gap' },
                { label: '可从备用 session 激活恢复', value: 'standby_recoverable' },
                { label: '未做过登录设备清理 / 外部设备未清理 / 最近设备清理失败', value: 'device_cleanup_missing' },
              ]}
              onChange={setAccountFilter}
            />
            {isProfileMode && <Button onClick={showProfileIncompleteAccounts}>只看待初始化</Button>}
            {isProfileMode && <Button disabled={!profileIncompleteAccounts.length} onClick={selectProfileIncompleteAccounts}>选择待初始化</Button>}
            {mode === 'standby_session' && <Button onClick={showStandbyGapAccounts}>只看备用 session 缺口</Button>}
            {mode === 'standby_session' && <Button onClick={selectStandbyGapAccounts}>选择备用 session 缺口</Button>}
            <Button onClick={() => setDraftAccountIds(filteredAccounts.map((account) => account.id))}>选择当前筛选</Button>
            <Button onClick={() => mergeDraftAccountIds(filteredAccounts.slice(0, BATCH_SELECTION_LIMIT).map((account) => account.id))}>选择当前筛选前 100 个</Button>
            <Button onClick={() => mergeDraftAccountIds(filteredAccounts.map((account) => account.id))}>追加当前筛选全部</Button>
            <Button disabled={!draftAccountIds.length} onClick={() => setDraftAccountIds([])}>清空本批选择</Button>
          </Space>
          <Space wrap>
            <Typography.Text type="secondary">区间选择</Typography.Text>
            <InputNumber
              min={1}
              max={Math.max(1, filteredAccounts.length)}
              value={rangeStart}
              addonBefore="从"
              onChange={(value) => setRangeStart(Number(value ?? 1))}
            />
            <InputNumber
              min={1}
              max={Math.max(1, filteredAccounts.length)}
              value={rangeEnd}
              addonBefore="到"
              onChange={(value) => setRangeEnd(Number(value ?? BATCH_SELECTION_LIMIT))}
            />
            <Button onClick={selectFilteredRange}>追加区间账号</Button>
            <Typography.Text type="secondary">按当前筛选结果顺序，包含起止序号。</Typography.Text>
          </Space>
          <Table<Account>
            className="tg-table"
            rowKey="id"
            columns={accountColumns}
            dataSource={filteredAccounts}
            rowSelection={{
              preserveSelectedRowKeys: true,
              selectedRowKeys: draftAccountIds,
              onChange: (keys) => setDraftAccountIds(keys.map(Number)),
            }}
            pagination={{ pageSize: 100, showSizeChanger: true, pageSizeOptions: ['50', '100', '200'], showTotal: (total, range) => `第 ${range[0]}-${range[1]} 条 / 共 ${total} 条` }}
            scroll={{ x: 820 }}
          />
        </Space>
        {isProfileMode && (
          <>
            <Divider />
            <Space orientation="vertical" size={12} style={{ width: '100%' }}>
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
                    { label: '随机头像包', value: 'random_from_material_pool' },
                    { label: '头像包顺序分配', value: 'sequential' },
                  ]}
                  onChange={changeAvatarMode}
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
              {avatarSourceHint && <Alert type="info" showIcon message={avatarSourceHint} />}
              {shouldShowAvatarSourceInput && (
                <Input.TextArea
                  rows={2}
                  value={avatarStrategy.avatar_sources.join('\n')}
                  placeholder="可选覆盖头像来源：每行一个 avatar:对象key / material:素材ID / 平台媒体文件路径"
                  onChange={(event) => setAvatarStrategy((current) => ({ ...current, avatar_sources: event.target.value.split(/\n+/).map((item) => item.trim()).filter(Boolean) }))}
                />
              )}
            </Space>
          </>
        )}
        {mode === 'standby_session' && (
          <>
            <Divider />
            <Space orientation="vertical" size={12} style={{ width: '100%' }}>
              <Typography.Text strong>备用 session 补齐策略</Typography.Text>
              <Space wrap>
                <Select
                  value={standbySlotStrategy}
                  style={{ width: 220 }}
                  options={[
                    { label: '自动补齐缺失槽位', value: 'auto_missing' },
                    { label: '仅 standby_1', value: 'standby_1' },
                    { label: '仅 standby_2', value: 'standby_2' },
                  ]}
                  onChange={setStandbySlotStrategy}
                />
                <Tag>任务中心：account_standby_session_provision</Tag>
              </Space>
              <Alert
                type="warning"
                showIcon
                message="预检会检查平台托管 2FA、开发者应用健康、代理健康、目标槽位和新登录限制。"
                description="执行时会读取 TG 官方验证码并写入登录流水；验证码不可读取、2FA 未托管、开发者应用异常、代理异常或 Telegram 限制都会进入失败原因，不会静默标记成功。"
              />
            </Space>
          </>
        )}
        {mode === 'cleanup_devices' && (
          <Alert
            type="warning"
            showIcon
            message="只保留当前 session、已确认 hash 的 primary / standby_1 / standby_2 和一个官方锚点设备"
            description="预检必须列出保留设备、预计清理外部设备和待确认设备；无法确认任一平台 session 授权设备 hash 时，当前账号不能继续一键清理。"
          />
        )}
        {mode === 'set_two_fa' && (
          <Alert
            type="warning"
            showIcon
            message="平台托管 2FA"
            description="未设置账号会写入平台托管 2FA；已设置且平台托管旧密码的账号可替换；旧密码未知的账号进入人工处理。"
          />
        )}
        <Input.TextArea rows={2} value={reason} placeholder="操作原因" onChange={(event) => setReason(event.target.value)} />
        {standbyNoExecutable && (
          <Alert
            type="warning"
            showIcon
            message="当前没有可自动补齐的备用 session"
            description="预检已经把本次账号全部拦截为需人工处理或跳过。请先处理红色校验项；账号列展示的是当前 TG 昵称和 username，不是本次生成的新资料。"
          />
        )}
        <Space wrap>
          <Button icon={<RefreshCcw size={16} />} loading={loading} onClick={runPrecheck}>{precheckButtonLabel}</Button>
          {isProfileMode && <Button icon={<Activity size={16} />} loading={loading} onClick={runPrecheck}>重抽全部</Button>}
          <Button
            type="primary"
            icon={<CheckCircle2 size={16} />}
            disabled={!precheck || ((precheck.summary.executable ?? 0) < 1 && editedPreviewIds.size < 1)}
            loading={loading}
            onClick={openCreateConfirm}
          >
            {confirmButtonLabel}
          </Button>
        </Space>
        {precheck && (
          <Space orientation="vertical" size={8} style={{ width: '100%' }}>
            <Typography.Text strong>预检汇总：共 {precheck.summary.total ?? 0} 个，可执行 {precheck.summary.executable ?? 0} 个，需等待 {precheck.summary.waiting ?? 0} 个，自动跳过 {autoSkippedCount} 个</Typography.Text>
            <Table<AccountSecurityPreviewItem>
              className="tg-table"
              rowKey="account_id"
              columns={precheckColumns}
              dataSource={precheck.items}
              pagination={{ pageSize: 50, showSizeChanger: true, pageSizeOptions: ['20', '50', '100'] }}
              scroll={{ x: 920 }}
            />
          </Space>
        )}
        {batch && (
          <Space orientation="vertical" size={8} style={{ width: '100%' }}>
            <Typography.Text strong>批次 #{batch.id}：{statusText(batch.status)}，成功 {batch.success_count}，失败 {batch.failed_count}，跳过 {batch.skipped_count}</Typography.Text>
            <Typography.Text type="secondary">trace_id：{batch.trace_id}</Typography.Text>
            <Table<AccountSecurityBatchItem>
              className="tg-table"
              rowKey="id"
              columns={itemColumns}
              dataSource={batch.items}
              pagination={{ pageSize: 50, showSizeChanger: true, pageSizeOptions: ['20', '50', '100'] }}
              scroll={{ x: 960 }}
            />
          </Space>
        )}
      </Space>
      <Modal
        title={`${confirmButtonLabel}？`}
        open={confirmOpen}
        okText="确认"
        cancelText="取消"
        confirmLoading={loading}
        onOk={createBatch}
        onCancel={() => setConfirmOpen(false)}
      >
        <Space orientation="vertical" size={8}>
          <Typography.Text>动作：{modeConfig.title}</Typography.Text>
          <Typography.Text>账号：共 {precheck?.summary.total ?? 0} 个，可执行 {precheck?.summary.executable ?? 0} 个，需等待 {precheck?.summary.waiting ?? 0} 个，自动跳过 {autoSkippedCount} 个。</Typography.Text>
          <Typography.Text>原因：{reason || modeConfig.reason}</Typography.Text>
        </Space>
      </Modal>
    </Drawer>
  );
}
