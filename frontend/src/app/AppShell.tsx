import React from 'react';
import {
  Activity,
  Database,
  LayoutDashboard,
  LockKeyhole,
  MessageSquareText,
  RefreshCcw,
  ShieldAlert,
  Smartphone,
  Users,
} from 'lucide-react';
import { Alert, App as AntdApp, Button, Card, Form, Input, Layout, Menu, Space, Tooltip, Typography } from 'antd';
import { AppProvider, useAppContext } from './context';
import { VIEW_ROUTES } from './routes';
import type { ChannelMessage, MessageSendingPrefill, OperationTarget, TaskCenterPrefill, TaskCenterTaskType } from './types';
import { api } from '../shared/api/client';
import { canView, hasPermission } from './utils';

const { Header, Sider, Content } = Layout;

const AppModals = React.lazy(() => import('./AppModals').then((module) => ({ default: module.AppModals })));
const OverviewView = React.lazy(() => import('./views/OverviewView'));
const AccountsView = React.lazy(() => import('./views/AccountsView'));
const SystemConfigView = React.lazy(() => import('./views/SystemConfigView'));
const UsageReportsView = React.lazy(() => import('./views/UsageReportsView'));
const GroupManagementView = React.lazy(() => import('./views/GroupManagementView'));
const AuditsView = React.lazy(() => import('./views/AuditsView'));
const OperationTargetsView = React.lazy(() => import('./views/OperationTargetsView'));
const TaskCenterView = React.lazy(() => import('./views/TaskCenterView'));
const MessageSendingView = React.lazy(() => import('./views/MessageSendingView'));
const MaterialsView = React.lazy(() => import('./views/MaterialsView'));
const ListenerCenterView = React.lazy(() => import('./views/ListenerCenterView'));
const RulesCenterView = React.lazy(() => import('./views/RulesCenterView'));
const RiskControlView = React.lazy(() => import('./views/RiskControlView'));
const ArchivesView = React.lazy(() => import('./views/ArchivesView'));
const AdminManualView = React.lazy(() => import('./views/AdminManualView'));

type ShellNavItem = [string, string, React.ReactNode];

const SHELL_NAV_ITEMS: ShellNavItem[] = [
  ['overview', '运营中心', <LayoutDashboard size={18} />],
  ['accounts', 'TG账号管理', <Smartphone size={18} />],
  ['targetManagement', '运营目标', <Users size={18} />],
  ['messageSending', '消息发送', <MessageSquareText size={18} />],
  ['materials', '素材中心', <Database size={18} />],
  ['taskManagement', '任务中心', <Activity size={18} />],
  ['listenerCenter', '监听中心', <RefreshCcw size={18} />],
  ['ruleCenter', '规则中心', <ShieldAlert size={18} />],
  ['riskControl', '风控中心', <ShieldAlert size={18} />],
  ['archives', '归档中心', <Database size={18} />],
  ['usageReports', '运营数据', <Activity size={18} />],
  ['systemConfig', '系统设置', <Database size={18} />],
  ['audits', '审计记录', <LockKeyhole size={18} />],
  ['adminManual', '操作手册', <Database size={18} />],
];

function noticeMessageType(notice: string): 'success' | 'error' | 'warning' | 'info' {
  if (/失败|异常|错误|过期|未连接|不能|请先|需先/.test(notice)) return 'error';
  if (/等待|扫码|验证码|二步验证|确认|排队|重试/.test(notice)) return 'warning';
  if (/成功|已|完成|新增|保存|同步|生成|兑换|提交|通过/.test(notice)) return 'success';
  return 'info';
}

function AppShell() {
  const { message } = AntdApp.useApp();
  const [messagePrefill, setMessagePrefill] = React.useState<MessageSendingPrefill | null>(null);
  const [taskCenterPrefill, setTaskCenterPrefill] = React.useState<TaskCenterPrefill | null>(null);
  const [taskCenterFocus, setTaskCenterFocus] = React.useState<{ taskId: string; nonce: number } | null>(null);
  const [targetFocus, setTargetFocus] = React.useState<{ targetId: number; nonce: number } | null>(null);
  const [systemConfigTab, setSystemConfigTab] = React.useState('developer-apps');
  const ctx = useAppContext();
  const {
    token, currentUser,
    loginEmail, setLoginEmail, loginPassword, setLoginPassword,
    login,
    captchaChallenge, captchaInput, setCaptchaInput,
    captchaToken, captchaError, captchaLoading, refreshCaptchaChallenge,
    activeView, goToView, busy, notice, setNotice, isActionPending,
    runtime, overview,
    accountPools, selectedPoolId, setSelectedPoolId, accounts, selectedPool,
    developerApps, tenants, adminUsers, groups, selectedGroup, selectedGroupId, setSelectedGroupId,
    tasks,
    archives, archiveDetail, audits, auditFilters, setAuditFilters, groupDetail,
    aiProviders, promptTemplates, tenantAiSetting, setTenantAiSetting, materials, materialCacheHealth, materialCacheConfig, materialImports, contentKeywordRules,
    usageLedgers, usageSummary,
    accountDetail, accountDetailTab, setAccountDetailTab,
    accountPoolDetail, poolDirectAccountId, setPoolDirectAccountId,
    cloneForm, setCloneForm, profileForm, setProfileForm, avatarFile, setAvatarFile,
    accountCreateForm, setAccountCreateForm, loginAfterCreate,
    accountPoolForm, setAccountPoolForm,
    developerAppForm, setDeveloperAppForm,
    tenantForm, setTenantForm,
    aiProviderForm, setAiProviderForm,
    promptTemplateForm, setPromptTemplateForm,
    materialForm, setMaterialForm, setMaterialFile, openPromptTemplateEdit, openMaterialEdit, disableMaterial, restoreMaterial, openContentKeywordRuleEdit,
    groupPolicy, setGroupPolicy,
    modal, setModal,
    directMessageForm, setDirectMessageForm,
    selectedDirectContact, accountContacts,
    returnAfterVerification, setReturnAfterVerification,
    refresh, openConfirm,
    openAccountCreate, openAccountDetail, openAccountVerificationCodes, openAccountMovePool, openAccountPoolDetail,
    refreshAccountPoolDetail, createAccount, createAccountPool, moveCurrentAccountPool,
    createClonePlan, confirmClonePlan, retryCloneItem,
    confirmVerificationTask, dismissVerificationTask,
    syncAccountContacts, queueAccountSyncNow,
    startDirectMessageToContact, createDirectMessageTask, createMessageSendTask,
    openGroupDetail,
    avatarUrl, openAccountProfileEdit, pollVerificationCodes,
    saveAccountProfile, retryAccountProfileSync,
    cancelTask, dispatchTask, drainQueue, retryTask,
    authorizeSelectedGroup, createArchive, saveGroupPolicy,
    openArchiveDetail, exportArchive, rerunArchive,
    createDeveloperApp, openDeveloperAppEdit, toggleDeveloperApp, checkDeveloperApp,
    openTenantEdit, saveTenantQuota, openAdminUserCreate, openAdminUserEdit,
    createAiProvider, openAiProviderEdit, toggleAiProvider, checkAiProvider,
    saveTenantAiSetting,
    createPromptTemplate, createMaterial,
    logout,
    runLogin, verifyAccount, deleteAccount, healthCheck, syncAccountGroups,
    accountName, groupName,
  } = ctx;

  const nav = currentUser ? SHELL_NAV_ITEMS.filter(([viewId]) => canView(currentUser, viewId)) : SHELL_NAV_ITEMS;

  React.useEffect(() => {
    if (!notice) return;
    void message.open({
      type: noticeMessageType(notice),
      content: notice,
      duration: 3,
    });
    setNotice('');
  }, [message, notice, setNotice]);

  React.useEffect(() => {
    if (!token || !currentUser || !nav.length) return;
    if (!nav.some(([viewId]) => viewId === activeView)) {
      goToView(nav[0][0]);
    }
  }, [activeView, currentUser, goToView, nav, token]);

  const loginReady = Boolean(
    loginEmail.trim()
    && loginPassword
    && captchaChallenge
    && (captchaToken || captchaInput.trim().length >= 5)
    && !captchaLoading
    && !isActionPending('auth:login'),
  );
  function openSendFromTarget(target: OperationTarget) {
    if (!hasPermission(currentUser, 'message_sending.manage')) return;
    setMessagePrefill({ target, nonce: Date.now() });
    goToView('messageSending');
  }

  function openTaskFromTarget(
    taskType: Extract<TaskCenterTaskType, 'group_ai_chat' | 'group_relay' | 'channel_view' | 'channel_like' | 'channel_comment'>,
    target: OperationTarget,
    channelMessage?: ChannelMessage,
  ) {
    setTaskCenterPrefill({ taskType, target, message: channelMessage, nonce: Date.now() });
    goToView('taskManagement');
  }

  function openTaskDetailFromOperation(taskId?: string) {
    if (taskId) {
      setTaskCenterFocus({ taskId, nonce: Date.now() });
    }
    goToView('taskManagement');
  }

  function openTargetDetailFromOperation(targetId?: number) {
    if (targetId) {
      setTargetFocus({ targetId, nonce: Date.now() });
    }
    goToView('targetManagement');
  }

  function openSystemConfig(tab: string = 'developer-apps') {
    setSystemConfigTab(tab);
    goToView('systemConfig');
  }

  async function openAccountDetailFromOperation(accountId: number) {
    const account = accounts.find((item) => item.id === accountId);
    await openAccountDetail((account ?? { id: accountId }) as Parameters<typeof openAccountDetail>[0]);
    setAccountDetailTab('可用性');
  }

  async function openTaskFromGroup(groupId?: number) {
    if (!groupId) {
      goToView('taskManagement');
      return;
    }
    try {
      const targets = await api<OperationTarget[]>('/operation-targets?target_type=group');
      const target = targets.find((item) => item.linked_group_id === groupId);
      if (target) {
        openTaskFromTarget('group_ai_chat', target);
        return;
      }
      void message.warning('该群还没有对应的运营目标，请先在运营目标中心同步或创建。');
    } catch {
      void message.warning('读取运营目标失败，请在任务中心手动选择目标。');
    }
    goToView('taskManagement');
  }

  const captchaControl = (
    <Card className={`captcha-box ${captchaToken ? 'verified' : ''}`} size="small">
      <div className="captcha-head">
        <span>验证码</span>
        <span className="captcha-refresh-hint">点击图片刷新</span>
      </div>
      <div className="captcha-code-row">
        <button
          type="button"
          className="captcha-image-button"
          onClick={refreshCaptchaChallenge}
          disabled={captchaLoading}
          aria-label="刷新验证码"
        >
          {captchaChallenge ? <img src={captchaChallenge.image_data_url} alt="验证码" /> : <div className="captcha-image-placeholder">加载中</div>}
        </button>
        <Input
          value={captchaInput}
          onChange={(event) => setCaptchaInput(event.target.value.toUpperCase())}
          disabled={!captchaChallenge || captchaLoading || Boolean(captchaToken)}
          placeholder="输入验证码"
          maxLength={5}
        />
      </div>
      <div className="captcha-actions">
        <span className={captchaToken ? 'captcha-ok' : captchaError ? 'captcha-error' : ''}>
          {captchaToken ? '验证码已通过，正在登录' : captchaError || '输入图片中的数字和字母'}
        </span>
      </div>
    </Card>
  );

  if (!token) {
    return (
      <div className="login-screen">
        <Card className="login-panel">
          <div className="brand login-brand">
            <div className="brand-mark">TG</div>
            <div>
              <strong>运营管理平台</strong>
              <span>单运营空间的 TG 账号运营中心</span>
            </div>
          </div>
          <Form layout="vertical">
            <Form.Item label="管理员账号">
              <Input value={loginEmail} onChange={(event) => setLoginEmail(event.target.value)} />
            </Form.Item>
            <Form.Item label="密码">
              <Input.Password value={loginPassword} onChange={(event) => setLoginPassword(event.target.value)} />
            </Form.Item>
            {captchaControl}
            <Button type="primary" block onClick={login} loading={isActionPending('auth:login') || captchaLoading} disabled={!loginReady}>登录运营中心</Button>
          </Form>
        </Card>
      </div>
    );
  }

  return (
    <Layout className="app-shell">
      <Sider className="sidebar" width={260}>
        <div className="brand">
          <div className="brand-mark">TG</div>
          <div>
            <strong>运营管理平台</strong>
            <span>TG 账号运营中心</span>
          </div>
        </div>
        <Menu
          className="shell-menu"
          mode="inline"
          selectedKeys={[activeView]}
          onClick={({ key }) => goToView(key)}
          items={nav.map(([id, label, icon]) => ({ key: id, label, icon }))}
        />
      </Sider>

      <Layout>
        <Header className="topbar">
          <div>
            <Typography.Text type="secondary">默认运营空间 / {currentUser?.role ?? '未加载角色'}</Typography.Text>
            <Typography.Title level={1}>{nav.find(([id]) => id === activeView)?.[1]}</Typography.Title>
            {currentUser && (
              <Typography.Text type="secondary">
                TG 账号资产、群频道目标与运营任务
              </Typography.Text>
            )}
          </div>
          <Space className="top-actions">
            {busy && <Typography.Text className="busy">{busy}...</Typography.Text>}
            <Tooltip title="刷新当前数据">
              <Button aria-label="刷新当前数据" icon={<RefreshCcw size={18} />} loading={isActionPending('app:refresh')} onClick={() => refresh()} />
            </Tooltip>
            <Button icon={<LockKeyhole size={16} />} onClick={logout}>退出</Button>
          </Space>
        </Header>

        <Content className="app-content">
        {runtime && hasPermission(currentUser, 'system.view') && activeView === 'systemConfig' && (
          <Alert
            className="runtime-strip"
            type="info"
            showIcon
            title="系统诊断"
            description={`任务通道：${runtime.queue_backend} / TG 连接：${runtime.telethon_configured ? '已配置' : '待配置'} / 应用池：${runtime.developer_app_healthy_count}/${runtime.developer_app_count} 正常 / AI 服务：${runtime.healthy_ai_provider_count}/${runtime.ai_provider_count} 正常${runtime.mock_ai_fallback_enabled ? ' / 可回退' : ''}`}
          />
        )}

        <React.Suspense fallback={<Card className="panel">加载中...</Card>}>
          {/* ===== View routing ===== */}
          {activeView === 'overview' && overview && (
            <OverviewView
              overview={overview}
              onOpenTargets={openTargetDetailFromOperation}
              onOpenTaskDetail={openTaskDetailFromOperation}
              onOpenMessageSending={() => goToView('messageSending')}
              onOpenAccounts={() => goToView('accounts')}
              onOpenAccountDetail={openAccountDetailFromOperation}
              onOpenRules={() => goToView('ruleCenter')}
              onOpenRisk={() => goToView('riskControl')}
              canManageOperationIssues={hasPermission(currentUser, 'operation_issues.manage')}
            />
          )}
          {activeView === 'systemConfig' && hasPermission(currentUser, 'system.view') && (
            <SystemConfigView
              developerApps={developerApps}
              tenants={tenants}
              aiProviders={aiProviders}
              promptTemplates={promptTemplates}
              tenantAiSetting={tenantAiSetting}
              materials={materials}
              materialCacheHealth={materialCacheHealth}
              materialCacheConfig={materialCacheConfig}
              contentKeywordRules={contentKeywordRules}
              adminUsers={adminUsers}
              currentUser={currentUser}
              currentUserRole={currentUser?.role}
              runtime={runtime}
              activeTab={systemConfigTab}
              onTabChange={setSystemConfigTab}
              onCreateDeveloperApp={() => setModal({ type: 'developerAppCreate' })}
              onEditDeveloperApp={openDeveloperAppEdit}
              onCheckDeveloperApp={checkDeveloperApp}
              onToggleDeveloperApp={toggleDeveloperApp}
              onEditTenant={openTenantEdit}
              onCreateAdminUser={openAdminUserCreate}
              onEditAdminUser={openAdminUserEdit}
              onCreateAiProvider={() => setModal({ type: 'aiProviderCreate' })}
              onEditAiProvider={openAiProviderEdit}
              onToggleAiProvider={toggleAiProvider}
              onCheckAiProvider={checkAiProvider}
              onEditTenantAi={() => setModal({ type: 'tenantAiEdit' })}
              onCreatePromptTemplate={() => {
                setPromptTemplateForm({
                  id: null,
                  name: '运营群活跃模板',
                  template_type: '群活跃对话计划',
                  content: '请为 {{group_title}} 围绕 {{topic}} 生成 {{count}} 条自然 Telegram 群聊发言计划，语气 {{tone}}，素材 {{materials}}，输出 JSON turns，并包含角色、意图、延迟和自动校验建议。',
                  is_active: true,
                });
                setModal({ type: 'promptTemplateCreate' });
              }}
              onCreateSlangTemplate={() => {
                setPromptTemplateForm({
                  id: null,
                  name: '默认 AI 黑话配置',
                  template_type: 'AI黑话词表',
                  content: '老师=妓女\n开课=开始营业',
                  is_active: true,
                });
                setModal({ type: 'promptTemplateCreate' });
              }}
              onEditPromptTemplate={openPromptTemplateEdit}
              onCreateMaterial={() => {
                setMaterialForm({
                  id: null,
                  title: '活动表情包',
                  material_type: '表情包',
                  content: 'https://example.local/stickers/welcome.webp',
                  tags: '表情包,欢迎',
                  emoji_asset_kind: 'image_meme',
                  cache_ready_status: 'not_cached',
                  delivery_mode: 'download_reupload',
                  source_kind: 'url',
                });
                setMaterialFile(null);
                setModal({ type: 'materialCreate' });
              }}
              onEditMaterial={openMaterialEdit}
              onCreateKeywordRule={() => setModal({ type: 'keywordRuleCreate' })}
              onEditKeywordRule={openContentKeywordRuleEdit}
              onSavedMaterialCacheConfig={refresh}
              onOpenConfirm={openConfirm}
              isActionPending={isActionPending}
            />
          )}
          {activeView === 'usageReports' && <UsageReportsView usageLedgers={usageLedgers} usageSummary={usageSummary} currentUser={currentUser} />}
          {activeView === 'accounts' && (
            <AccountsView accounts={accounts} accountPools={accountPools} selectedPoolId={selectedPoolId} setSelectedPoolId={setSelectedPoolId} selectedPool={selectedPool ?? undefined} avatarUrl={avatarUrl} runtime={runtime} onConfigureDeveloperApps={() => openSystemConfig('developer-apps')} onCreatePoolClick={() => setModal({ type: 'accountPoolCreate' })} onCreateAccount={openAccountCreate} onOpenPoolDetail={openAccountPoolDetail} onOpenAccountDetail={openAccountDetail} onExtractCodes={openAccountVerificationCodes} onMovePool={openAccountMovePool} onRunLogin={runLogin} onVerifyAccount={verifyAccount} onDeleteAccount={(account) => openConfirm({ title: '移除账号', message: `确认移除 ${account.display_name}？历史任务、群归档和审计记录会保留，手机号可以重新新增。`, confirmLabel: '移除账号', tone: 'danger', onConfirm: () => deleteAccount(account) })} onHealthCheck={healthCheck} onSyncGroups={syncAccountGroups} isActionPending={isActionPending} canCreateAccount={hasPermission(currentUser, 'accounts.create')} canLoginAccount={hasPermission(currentUser, 'accounts.login')} canSyncAccount={hasPermission(currentUser, 'accounts.sync')} canViewCodes={hasPermission(currentUser, 'accounts.codes.read')} canSecurityRead={hasPermission(currentUser, 'accounts.security.read')} canSecurityBatch={hasPermission(currentUser, 'accounts.security.batch')} canProfileBatchUpdate={hasPermission(currentUser, 'accounts.profile.batch_update')} canMovePool={hasPermission(currentUser, 'accounts.pool_manage')} canDeleteAccount={hasPermission(currentUser, 'accounts.delete')} />
          )}
          {activeView === 'targetManagement' && (
            <OperationTargetsView
              onSendToTarget={openSendFromTarget}
              onCreateTaskFromTarget={openTaskFromTarget}
              focusTarget={targetFocus}
              onFocusTargetConsumed={() => setTargetFocus(null)}
              canManageMessageSending={hasPermission(currentUser, 'message_sending.manage')}
              canManageTargets={hasPermission(currentUser, 'targets.manage')}
              canManageTasks={hasPermission(currentUser, 'tasks.manage')}
              canManageArchives={hasPermission(currentUser, 'archives.manage')}
            />
          )}
          {activeView === 'messageSending' && (
            <MessageSendingView
              accounts={accounts}
              materials={materials}
              tasks={tasks}
              prefill={messagePrefill}
              createMessageSendTask={createMessageSendTask}
              onCancelTask={cancelTask}
              onDispatchTask={dispatchTask}
              onRetryTask={retryTask}
              onRefresh={refresh}
              isActionPending={isActionPending}
              canManageMessageSending={hasPermission(currentUser, 'message_sending.manage')}
            />
          )}
          {activeView === 'materials' && hasPermission(currentUser, 'materials.view') && (
            <MaterialsView
              materials={materials}
              materialImports={materialImports}
              materialCacheHealth={materialCacheHealth}
              canUploadMaterials={hasPermission(currentUser, 'materials.upload')}
              canManageMaterials={hasPermission(currentUser, 'materials.manage')}
              onCreateMaterial={() => {
                setMaterialForm({
                  id: null,
                  title: '活动表情包',
                  material_type: '表情包',
                  content: 'https://example.local/stickers/welcome.webp',
                  tags: '表情包,欢迎',
                  emoji_asset_kind: 'image_meme',
                  cache_ready_status: 'not_cached',
                  delivery_mode: 'download_reupload',
                  source_kind: 'upload',
                });
                setMaterialFile(null);
                setModal({ type: 'materialCreate' });
              }}
              onEditMaterial={openMaterialEdit}
              onDisableMaterial={disableMaterial}
              onRestoreMaterial={restoreMaterial}
              onOpenImportResult={(result) => setModal({ type: 'materialImportResult', payload: result })}
              onRefresh={refresh}
              isActionPending={isActionPending}
            />
          )}
          {activeView === 'groupManagement' && (
            <GroupManagementView groups={groups} selectedGroup={selectedGroup ?? undefined} selectedGroupId={selectedGroupId} groupDetail={groupDetail} setSelectedGroupId={setSelectedGroupId} archives={archives} archiveDetail={archiveDetail} onCreateTask={openTaskFromGroup} onCreateArchive={createArchive} onAuthorizeGroup={authorizeSelectedGroup} onEditGroupPolicy={() => setModal({ type: 'groupPolicyEdit' })} onOpenGroupDetail={openGroupDetail} onOpenArchiveDetail={openArchiveDetail} onExportArchive={exportArchive} onRerunArchive={rerunArchive} onOpenConfirm={openConfirm} isActionPending={isActionPending} />
          )}
          {activeView === 'taskManagement' && (
            <TaskCenterView
              accounts={accounts}
              accountPools={accountPools}
              promptTemplates={promptTemplates}
              prefill={taskCenterPrefill}
              focusTask={taskCenterFocus}
              onFocusTaskConsumed={() => setTaskCenterFocus(null)}
              canManageTasks={hasPermission(currentUser, 'tasks.manage')}
              canDispatchControl={hasPermission(currentUser, 'tasks.dispatch_control')}
            />
          )}
          {activeView === 'listenerCenter' && <ListenerCenterView canManageListeners={hasPermission(currentUser, 'listeners.manage')} />}
          {activeView === 'ruleCenter' && <RulesCenterView onOpenSystemConfig={() => openSystemConfig('resources')} />}
          {activeView === 'riskControl' && (
            <RiskControlView
              onOpenAccounts={() => goToView('accounts')}
              canManageRisk={hasPermission(currentUser, 'risk.manage')}
              canManageProxies={hasPermission(currentUser, 'proxies.manage')}
            />
          )}
          {activeView === 'archives' && <ArchivesView archives={archives} archiveDetail={archiveDetail} onOpenArchiveDetail={openArchiveDetail} onExportArchive={exportArchive} onRerunArchive={rerunArchive} onRefresh={refresh} isActionPending={isActionPending} />}
          {activeView === 'audits' && <AuditsView audits={audits} filters={auditFilters} setFilters={setAuditFilters} onRefresh={refresh} canExport={hasPermission(currentUser, 'audit.export')} />}
          {activeView === 'adminManual' && <AdminManualView />}
        </React.Suspense>

        {/* ===== Modals ===== */}
        {modal && (
          <React.Suspense fallback={null}>
            <AppModals />
          </React.Suspense>
        )}
        </Content>
      </Layout>
    </Layout>
  );
}

export default function App() {
  return (
    <AppProvider>
      <AppShell />
    </AppProvider>
  );
}
