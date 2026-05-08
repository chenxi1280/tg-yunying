import React from 'react';
import {
  Activity,
  CheckCircle2,
  Database,
  LayoutDashboard,
  LockKeyhole,
  RefreshCcw,
  ShieldAlert,
  Smartphone,
  Users,
} from 'lucide-react';
import { Alert, App as AntdApp, Button, Card, Form, Input, Layout, Menu, Space, Tabs, Typography } from 'antd';
import { AppProvider, useAppContext } from './context';
import OverviewView from './views/OverviewView';
import AccountsView from './views/AccountsView';
import CampaignsView from './views/CampaignsView';
import SystemConfigView from './views/SystemConfigView';
import UsageReportsView from './views/UsageReportsView';
import GroupManagementView from './views/GroupManagementView';
import AuditsView from './views/AuditsView';
import { AppModals } from './AppModals';
import { VIEW_ROUTES } from './routes';

const { Header, Sider, Content } = Layout;

function noticeMessageType(notice: string): 'success' | 'error' | 'warning' | 'info' {
  if (/失败|异常|错误|过期|未连接|不能|请先|需先/.test(notice)) return 'error';
  if (/等待|扫码|验证码|二步验证|确认|排队|重试/.test(notice)) return 'warning';
  if (/成功|已|完成|新增|保存|同步|生成|兑换|提交|通过/.test(notice)) return 'success';
  return 'info';
}

function AppShell() {
  const { message } = AntdApp.useApp();
  const ctx = useAppContext();
  const {
    token, currentUser, authMode, setAuthMode,
    loginEmail, setLoginEmail, loginPassword, setLoginPassword,
    registerForm, setRegisterForm, login, register,
    captchaChallenge, captchaInput, setCaptchaInput,
    captchaToken, captchaError, captchaLoading, refreshCaptchaChallenge, verifyCaptcha,
    activeView, goToView, busy, notice, setNotice, isActionPending,
    runtime, overview, redeemCode, setRedeemCode, submitRedeemCode,
    accountPools, selectedPoolId, setSelectedPoolId, accounts, selectedPool,
    developerApps, tenants, subscriptionPlans, adminUsers, groups, selectedGroup, selectedGroupId, setSelectedGroupId,
    campaigns, selectedCampaign, selectedCampaignId, setSelectedCampaignId,
    drafts, tasks, taskManagementTab, setTaskManagementTab,
    taskSummary, selectedCampaignDrafts, selectedCampaignTasks,
    taskStatusFilter, setTaskStatusFilter,
    archives, archiveDetail, audits, groupDetail,
    aiProviders, promptTemplates, tenantAiSetting, setTenantAiSetting, schedulingSetting, materials, contentKeywordRules,
    activationCodes, activationCodePage, activationCodeFilters, setActivationCodeFilters,
    activationBatch, setActivationBatch,
    usageLedgers, usageSummary,
    accountDetail, accountDetailTab, setAccountDetailTab,
    accountPoolDetail, poolDirectAccountId, setPoolDirectAccountId,
    cloneForm, setCloneForm, profileForm, setProfileForm, avatarFile, setAvatarFile,
    draftEditTarget, draftEditForm, setDraftEditForm,
    accountCreateForm, setAccountCreateForm, loginAfterCreate,
    accountPoolForm, setAccountPoolForm,
    developerAppForm, setDeveloperAppForm,
    tenantForm, setTenantForm,
    aiProviderForm, setAiProviderForm,
    promptTemplateForm, setPromptTemplateForm,
    materialForm, setMaterialForm, openContentKeywordRuleEdit,
    groupPolicy, setGroupPolicy,
    modal, setModal, resultDialog, setResultDialog,
    selectedTargetGroupIds, recommendedAccounts, selectedAccountsByGroup,
    targetGroupsMissingAccounts,
    campaignStep, setCampaignStep,
    topic, setTopic, sendWindow, setSendWindow,
    intensity, setIntensity, draftCount, setDraftCount,
    tone, setTone, selectedMaterialIds,
    jitterMinSeconds, setJitterMinSeconds,
    jitterMaxSeconds, setJitterMaxSeconds,
    batchIntervalSeconds, setBatchIntervalSeconds,
    respectSendWindow, setRespectSendWindow,
    selectedAiProviderId, setSelectedAiProviderId,
    directMessageForm, setDirectMessageForm,
    selectedDirectContact, accountContacts,
    returnAfterVerification, setReturnAfterVerification,
    refresh, showResult, closeModal, openConfirm,
    openCampaignModal, openAccountCreate, openAccountDetail, openAccountPoolDetail,
    refreshAccountPoolDetail, createAccount, createAccountPool, moveCurrentAccountPool,
    createClonePlan, confirmClonePlan, retryCloneItem,
    confirmVerificationTask, dismissVerificationTask,
    syncAccountContacts, queueAccountSyncNow,
    startDirectMessageToContact, createDirectMessageTask,
    openGroupDetail, openDraftEdit, saveDraftEdit,
    avatarUrl, openAccountProfileEdit, pollVerificationCodes,
    saveAccountProfile, retryAccountProfileSync,
    toggleTargetGroup, toggleRecommendedAccount, setGroupAccountsSelected,
    goCampaignAccountStep, goCampaignContentStep,
    createCampaignAndDrafts, approveDraft, approveAllDrafts,
    cancelCampaign,
    dispatchTask, drainQueue, retryTask,
    authorizeSelectedGroup, createArchive, saveGroupPolicy,
    openArchiveDetail, exportArchive, rerunArchive,
    createDeveloperApp, openDeveloperAppEdit, toggleDeveloperApp, checkDeveloperApp,
    openTenantEdit, saveTenantQuota,
    createSubscriptionPlan, openSubscriptionPlanEdit, openAdminUserEdit,
    createAiProvider, openAiProviderEdit, toggleAiProvider, checkAiProvider,
    saveTenantAiSetting, saveSchedulingSetting,
    createPromptTemplate, createMaterial, toggleMaterial,
    loadActivationCodes, createActivationCodes, disableActivationCode, logout,
    runLogin, verifyAccount, deleteAccount, healthCheck, syncAccountGroups,
    accountName, groupName,
  } = ctx;

  const menuPermissions = currentUser?.menu_permissions ?? [];
  const canSeeMenu = (viewId: string) => currentUser?.role === '系统管理员'
    || viewId === 'overview'
    || menuPermissions.includes('*')
    || menuPermissions.includes(viewId);
  const navCandidates: Array<[string, string, React.ReactNode]> = [
    ['overview', '运营概览', <LayoutDashboard size={18} />],
  ];
  if (currentUser?.role === '系统管理员') {
    navCandidates.push(['systemConfig', '系统配置', <Database size={18} />]);
  }
  navCandidates.push(
    ['accounts', '账号管理', <Smartphone size={18} />],
    ['groupManagement', '群聊管理', <Users size={18} />],
    ['taskManagement', '任务管理', <Activity size={18} />],
  );
  if (currentUser?.role !== '系统管理员') {
    navCandidates.push(['usageReports', '用量余额', <Activity size={18} />]);
  }
  navCandidates.push(['audits', '审计安全', <LockKeyhole size={18} />]);
  const nav = navCandidates.filter(([viewId]) => canSeeMenu(viewId));

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

  const loginReady = Boolean(loginEmail.trim() && loginPassword && captchaToken && !isActionPending('auth:login'));
  const registerReady = Boolean(registerForm.name.trim() && registerForm.email.trim() && registerForm.password && captchaToken && !isActionPending('auth:register'));

  const captchaControl = (
    <Card className={`captcha-box ${captchaToken ? 'verified' : ''}`} size="small">
      <div className="captcha-head">
        <span>验证码</span>
        <Button size="small" loading={captchaLoading} onClick={refreshCaptchaChallenge} disabled={captchaLoading}>
          刷新
        </Button>
      </div>
      <div className="captcha-code-row">
        {captchaChallenge ? <img src={captchaChallenge.image_data_url} alt="验证码" /> : <div className="captcha-image-placeholder">加载中</div>}
        <Input
          value={captchaInput}
          onChange={(event) => setCaptchaInput(event.target.value.toUpperCase())}
          disabled={!captchaChallenge || captchaLoading || Boolean(captchaToken)}
          placeholder="输入验证码"
          maxLength={5}
        />
      </div>
      <div className="captcha-actions">
        <Button size="small" loading={captchaLoading} onClick={verifyCaptcha} disabled={!captchaChallenge || captchaLoading || Boolean(captchaToken) || captchaInput.trim().length < 5}>
          {captchaToken ? '已通过' : captchaLoading ? '验证中' : '验证'}
        </Button>
        <span className={captchaToken ? 'captcha-ok' : captchaError ? 'captcha-error' : ''}>
          {captchaToken ? '验证码已通过' : captchaError || '输入图片中的数字和字母'}
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
              <span>登录后按租户隔离账号、群和任务</span>
            </div>
          </div>
          <Tabs
            activeKey={authMode}
            onChange={(key) => setAuthMode(key as 'login' | 'register')}
            items={[
              {
                key: 'login',
                label: '登录',
                children: (
                  <Form layout="vertical">
                    <Form.Item label="用户名、邮箱或手机号">
                      <Input value={loginEmail} onChange={(event) => setLoginEmail(event.target.value)} />
                    </Form.Item>
                    <Form.Item label="密码">
                      <Input.Password value={loginPassword} onChange={(event) => setLoginPassword(event.target.value)} />
                    </Form.Item>
                    {captchaControl}
                    <Button type="primary" block onClick={login} loading={isActionPending('auth:login')} disabled={!loginReady}>登录控制台</Button>
                  </Form>
                ),
              },
              {
                key: 'register',
                label: '注册',
                children: (
                  <Form layout="vertical">
                    <Form.Item label="用户名">
                      <Input value={registerForm.name} onChange={(event) => setRegisterForm((current) => ({ ...current, name: event.target.value }))} />
                    </Form.Item>
                    <Form.Item label="邮箱">
                      <Input value={registerForm.email} onChange={(event) => setRegisterForm((current) => ({ ...current, email: event.target.value }))} />
                    </Form.Item>
                    <Form.Item label="手机号">
                      <Input value={registerForm.phone} onChange={(event) => setRegisterForm((current) => ({ ...current, phone: event.target.value }))} />
                    </Form.Item>
                    <Form.Item label="密码">
                      <Input.Password value={registerForm.password} onChange={(event) => setRegisterForm((current) => ({ ...current, password: event.target.value }))} />
                    </Form.Item>
                    {captchaControl}
                    <Button type="primary" block onClick={register} loading={isActionPending('auth:register')} disabled={!registerReady}>创建普通用户</Button>
                  </Form>
                ),
              },
            ]}
          />
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
            <span>多客户代运营控制台</span>
          </div>
        </div>
        <Menu
          className="shell-menu"
          mode="inline"
          selectedKeys={[activeView]}
          onClick={({ key }) => goToView(key)}
          items={nav.map(([id, label, icon]) => ({ key: id, label, icon }))}
        />
        <Card className="side-note" size="small">
          <ShieldAlert size={18} />
          <span>默认半自动审核，只在可运营群内执行，验证码查看与发送动作都有记录。</span>
        </Card>
      </Sider>

      <Layout>
        <Header className="topbar">
          <div>
            <Typography.Text type="secondary">{currentUser?.tenant_name ?? '试运行租户'} / {currentUser?.role ?? '未加载角色'}</Typography.Text>
            <Typography.Title level={1}>{nav.find(([id]) => id === activeView)?.[1]}</Typography.Title>
            {currentUser && (
              <Typography.Text type="secondary">
                订阅：{currentUser.subscription_status} / 剩余 {currentUser.subscription_days_remaining} 天
                {currentUser.role !== '系统管理员' ? ` / Token ${currentUser.token_balance.toLocaleString()}` : ''}
              </Typography.Text>
            )}
          </div>
          <Space className="top-actions">
            {busy && <Typography.Text className="busy">{busy}...</Typography.Text>}
            <Button icon={<RefreshCcw size={18} />} loading={isActionPending('app:refresh')} onClick={() => refresh()} />
            <Button onClick={() => setModal({ type: 'changePassword' })}>修改密码</Button>
            <Button onClick={logout}>退出</Button>
          </Space>
        </Header>

        <Content className="app-content">
        {currentUser && currentUser.role !== '系统管理员' && (
          <Card className="panel" title="订阅状态" extra={<Typography.Text type="secondary">{currentUser.can_use_core_features ? '当前可正常使用核心功能' : '当前仅可查看数据，需先激活或续费'}</Typography.Text>}>
            <div className="summary-grid">
              <Card className="summary-card" size="small">
                <span>当前状态</span>
                <strong>{currentUser.subscription_status}</strong>
                <p>到期时间 {currentUser.subscription_expires_at ?? '未激活'}</p>
              </Card>
              <Card className="summary-card" size="small">
                <span>Token 余额</span>
                <strong>{currentUser.token_balance.toLocaleString()}</strong>
                <p>累计额度 {currentUser.token_quota_total.toLocaleString()}</p>
              </Card>
              <Card className="summary-card" size="small">
                <span>任务/消息</span>
                <strong>{taskSummary.campaigns} / {taskSummary.sent}</strong>
                <p>失败 {taskSummary.failed}，排队 {taskSummary.queued}</p>
              </Card>
              <Card className="summary-card" size="small">
                <span>卡密兑换</span>
                <strong>订阅 + Token</strong>
                <Space.Compact>
                  <Input value={redeemCode} onChange={(event) => setRedeemCode(event.target.value)} placeholder="请输入卡密" />
                  <Button type="primary" loading={isActionPending('subscription:redeem')} onClick={submitRedeemCode}>兑换</Button>
                </Space.Compact>
              </Card>
            </div>
          </Card>
        )}

        {runtime && currentUser?.role === '系统管理员' && activeView === 'systemConfig' && (
          <Alert
            className="runtime-strip"
            type="info"
            showIcon
            message="系统诊断"
            description={`任务通道：${runtime.queue_backend} / TG 连接：${runtime.telethon_configured ? '已配置' : '待配置'} / 应用池：${runtime.developer_app_healthy_count}/${runtime.developer_app_count} 正常 / AI 服务：${runtime.healthy_ai_provider_count}/${runtime.ai_provider_count} 正常${runtime.mock_ai_fallback_enabled ? ' / 可回退' : ''}`}
          />
        )}

        {/* ===== View routing ===== */}
        {activeView === 'overview' && overview && <OverviewView overview={overview} runtime={runtime} />}
        {activeView === 'systemConfig' && currentUser?.role === '系统管理员' && (
          <SystemConfigView
            developerApps={developerApps}
            tenants={tenants}
            subscriptionPlans={subscriptionPlans}
            adminUsers={adminUsers}
            aiProviders={aiProviders}
            promptTemplates={promptTemplates}
            tenantAiSetting={tenantAiSetting}
            schedulingSetting={schedulingSetting}
            materials={materials}
            contentKeywordRules={contentKeywordRules}
            activationCodes={activationCodes}
            activationCodePage={activationCodePage}
            activationCodeFilters={activationCodeFilters}
            setActivationCodeFilters={setActivationCodeFilters}
            activationBatch={activationBatch}
            setActivationBatch={setActivationBatch}
            usageLedgers={usageLedgers}
            usageSummary={usageSummary}
            currentUserRole={currentUser?.role}
            onCreateDeveloperApp={() => setModal({ type: 'developerAppCreate' })}
            onEditDeveloperApp={openDeveloperAppEdit}
            onCheckDeveloperApp={checkDeveloperApp}
            onToggleDeveloperApp={toggleDeveloperApp}
            onEditTenant={openTenantEdit}
            onCreateSubscriptionPlan={() => setModal({ type: 'subscriptionPlanCreate' })}
            onEditSubscriptionPlan={openSubscriptionPlanEdit}
            onEditAdminUser={openAdminUserEdit}
            onCreateAiProvider={() => setModal({ type: 'aiProviderCreate' })}
            onEditAiProvider={openAiProviderEdit}
            onToggleAiProvider={toggleAiProvider}
            onCheckAiProvider={checkAiProvider}
            onEditTenantAi={() => setModal({ type: 'tenantAiEdit' })}
            onEditScheduling={() => setModal({ type: 'schedulingEdit' })}
            onCreatePromptTemplate={() => setModal({ type: 'promptTemplateCreate' })}
            onCreateMaterial={() => setModal({ type: 'materialCreate' })}
            onCreateKeywordRule={() => setModal({ type: 'keywordRuleCreate' })}
            onEditKeywordRule={openContentKeywordRuleEdit}
            onLoadCodes={loadActivationCodes}
            onCreateCodes={createActivationCodes}
            onDisableCode={disableActivationCode}
            onOpenConfirm={openConfirm}
            isActionPending={isActionPending}
          />
        )}
        {activeView === 'usageReports' && <UsageReportsView usageLedgers={usageLedgers} usageSummary={usageSummary} currentUser={currentUser} />}
        {activeView === 'accounts' && (
          <AccountsView accounts={accounts} accountPools={accountPools} selectedPoolId={selectedPoolId} setSelectedPoolId={setSelectedPoolId} selectedPool={selectedPool ?? undefined} avatarUrl={avatarUrl} runtime={runtime} onConfigureDeveloperApps={() => goToView('systemConfig')} onCreatePoolClick={() => setModal({ type: 'accountPoolCreate' })} onCreateAccount={openAccountCreate} onOpenPoolDetail={openAccountPoolDetail} onOpenAccountDetail={openAccountDetail} onRunLogin={runLogin} onVerifyAccount={verifyAccount} onDeleteAccount={(account) => openConfirm({ title: '移除账号', message: `确认移除 ${account.display_name}？历史任务、群归档和审计记录会保留，手机号可以重新新增。`, confirmLabel: '移除账号', tone: 'danger', onConfirm: () => deleteAccount(account) })} onHealthCheck={healthCheck} onSyncGroups={syncAccountGroups} isActionPending={isActionPending} />
        )}
        {activeView === 'groupManagement' && (
          <GroupManagementView groups={groups} selectedGroup={selectedGroup ?? undefined} selectedGroupId={selectedGroupId} groupDetail={groupDetail} setSelectedGroupId={setSelectedGroupId} archives={archives} archiveDetail={archiveDetail} onCreateCampaign={openCampaignModal} onCreateArchive={createArchive} onAuthorizeGroup={authorizeSelectedGroup} onEditGroupPolicy={() => setModal({ type: 'groupPolicyEdit' })} onOpenGroupDetail={openGroupDetail} onOpenArchiveDetail={openArchiveDetail} onExportArchive={exportArchive} onRerunArchive={rerunArchive} onOpenConfirm={openConfirm} isActionPending={isActionPending} />
        )}
        {activeView === 'taskManagement' && (
          <CampaignsView campaigns={campaigns} tasks={tasks} drafts={drafts} groups={groups} accounts={accounts} taskManagementTab={taskManagementTab} setTaskManagementTab={setTaskManagementTab} taskSummary={taskSummary} selectedCampaign={selectedCampaign ?? undefined} selectedCampaignDrafts={selectedCampaignDrafts} selectedCampaignTasks={selectedCampaignTasks} taskStatusFilter={taskStatusFilter} setTaskStatusFilter={setTaskStatusFilter} setSelectedCampaignId={setSelectedCampaignId} onCreateCampaign={() => openCampaignModal()} onCancelCampaign={cancelCampaign} onApproveDraft={approveDraft} onApproveAllDrafts={approveAllDrafts} onDispatchTask={dispatchTask} onRetryTask={retryTask} onDrainQueue={drainQueue} onOpenConfirm={openConfirm} groupName={groupName} accountName={accountName} isActionPending={isActionPending} />
        )}
        {activeView === 'audits' && <AuditsView audits={audits} />}

        {/* ===== Modals ===== */}
        <AppModals />
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
