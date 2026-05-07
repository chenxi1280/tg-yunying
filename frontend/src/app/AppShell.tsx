import React from 'react';
import {
  Activity,
  Archive,
  Bot,
  CheckCircle2,
  Database,
  LayoutDashboard,
  LockKeyhole,
  RefreshCcw,
  ShieldAlert,
  Smartphone,
  Users,
} from 'lucide-react';
import { AppProvider, useAppContext } from './context';
import { StatusBadge } from './components/shared';
import { statusAccent, operationLabel } from './utils';
import OverviewView from './views/OverviewView';
import AccountsView from './views/AccountsView';
import GroupsView from './views/GroupsView';
import CampaignsView from './views/CampaignsView';
import DeveloperAppsView from './views/DeveloperAppsView';
import AISettingsView from './views/AISettingsView';
import ActivationCodesView from './views/ActivationCodesView';
import UsageReportsView from './views/UsageReportsView';
import ArchivesView from './views/ArchivesView';
import AuditsView from './views/AuditsView';
import { AppModals } from './AppModals';
import { VIEW_ROUTES, viewFromPath } from './routes';

function AppShell() {
  const ctx = useAppContext();
  const {
    token, currentUser, authMode, setAuthMode,
    loginEmail, setLoginEmail, loginPassword, setLoginPassword,
    registerForm, setRegisterForm, login, register,
    activeView, goToView, busy, notice, setNotice,
    runtime, overview, redeemCode, setRedeemCode, submitRedeemCode,
    accountPools, selectedPoolId, setSelectedPoolId, accounts, selectedPool,
    developerApps, tenants, groups, selectedGroup, selectedGroupId, setSelectedGroupId,
    campaigns, selectedCampaign, selectedCampaignId, setSelectedCampaignId,
    drafts, tasks, taskManagementTab, setTaskManagementTab,
    taskSummary, selectedCampaignDrafts, selectedCampaignTasks,
    taskStatusFilter, setTaskStatusFilter,
    archives, archiveDetail, audits,
    aiProviders, promptTemplates, tenantAiSetting, setTenantAiSetting, schedulingSetting, materials,
    activationCodes, activationBatch, setActivationBatch,
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
    materialForm, setMaterialForm,
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
    dispatchTask, drainQueue, retryTask,
    authorizeSelectedGroup, createArchive, saveGroupPolicy,
    openArchiveDetail,
    createDeveloperApp, toggleDeveloperApp, checkDeveloperApp,
    openTenantEdit, saveTenantQuota,
    createAiProvider, checkAiProvider,
    saveTenantAiSetting, saveSchedulingSetting,
    createPromptTemplate, createMaterial, toggleMaterial,
    createActivationCodes, logout,
    runLogin, verifyAccount, healthCheck, syncAccountGroups,
    accountName, groupName,
  } = ctx;

  const nav: Array<[string, string, React.ReactNode]> = [
    ['overview', '运营概览', <LayoutDashboard size={18} />],
    ...(currentUser?.role === '系统管理员'
      ? [
          ['developerApps', '开发者应用', <Database size={18} />] as [string, string, React.ReactNode],
          ['aiSettings', 'AI 配置', <Bot size={18} />] as [string, string, React.ReactNode],
          ['activationCodes', '卡密管理', <LockKeyhole size={18} />] as [string, string, React.ReactNode],
          ['usageReports', '用户用量', <Activity size={18} />] as [string, string, React.ReactNode],
        ]
      : []),
    ['accounts', '账号池', <Smartphone size={18} />],
    ['groups', '群聊库', <Users size={18} />],
    ['taskManagement', '任务管理', <Activity size={18} />],
    ['archives', '群聊归档', <Archive size={18} />],
    ['audits', '审计安全', <LockKeyhole size={18} />],
  ];

  if (!token) {
    return (
      <div className="login-screen">
        <section className="login-panel">
          <div className="brand login-brand">
            <div className="brand-mark">TG</div>
            <div>
              <strong>运营管理平台</strong>
              <span>登录后按租户隔离账号、群和任务</span>
            </div>
          </div>
          <div className="tabs-row">
            <button className={authMode === 'login' ? 'active' : ''} onClick={() => setAuthMode('login')}>登录</button>
            <button className={authMode === 'register' ? 'active' : ''} onClick={() => setAuthMode('register')}>注册</button>
          </div>
          {authMode === 'login' ? (
            <>
              <label>
                邮箱或手机号
                <input value={loginEmail} onChange={(event) => setLoginEmail(event.target.value)} />
              </label>
              <label>
                密码
                <input type="password" value={loginPassword} onChange={(event) => setLoginPassword(event.target.value)} />
              </label>
              <button className="primary" onClick={login}>登录控制台</button>
            </>
          ) : (
            <>
              <label>
                用户名
                <input value={registerForm.name} onChange={(event) => setRegisterForm((current) => ({ ...current, name: event.target.value }))} />
              </label>
              <label>
                邮箱
                <input value={registerForm.email} onChange={(event) => setRegisterForm((current) => ({ ...current, email: event.target.value }))} />
              </label>
              <label>
                手机号
                <input value={registerForm.phone} onChange={(event) => setRegisterForm((current) => ({ ...current, phone: event.target.value }))} />
              </label>
              <label>
                密码
                <input type="password" value={registerForm.password} onChange={(event) => setRegisterForm((current) => ({ ...current, password: event.target.value }))} />
              </label>
              <button className="primary" onClick={register}>创建普通用户</button>
            </>
          )}
          <div className="demo-accounts">
            <button onClick={() => { setLoginEmail('admin@demo.local'); setLoginPassword('admin123'); }}>平台管理员</button>
            <button onClick={() => { setLoginEmail('ops@demo.local'); setLoginPassword('ops123'); }}>演示普通用户</button>
          </div>
          {notice && <p className="login-error">{notice}</p>}
        </section>
      </div>
    );
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">TG</div>
          <div>
            <strong>运营管理平台</strong>
            <span>多客户代运营控制台</span>
          </div>
        </div>
        <nav>
          {nav.map(([id, label, icon]) => (
            <button key={id} className={activeView === id ? 'active' : ''} onClick={() => goToView(id)}>
              {icon}
              {label}
            </button>
          ))}
        </nav>
        <div className="side-note">
          <ShieldAlert size={18} />
          <span>默认半自动审核，只在可运营群内执行，验证码查看与发送动作都有记录。</span>
        </div>
      </aside>

      <main>
        <header className="topbar">
          <div>
            <p>{currentUser?.tenant_name ?? '试运行租户'} / {currentUser?.role ?? '未加载角色'}</p>
            <h1>{nav.find(([id]) => id === activeView)?.[1]}</h1>
            {currentUser && <span>订阅：{currentUser.subscription_status} / 剩余 {currentUser.subscription_days_remaining} 天</span>}
          </div>
          <div className="top-actions">
            {busy && <span className="busy">{busy}...</span>}
            <button className="icon-button" onClick={() => refresh()}>
              <RefreshCcw size={18} />
            </button>
            <button onClick={logout}>退出</button>
          </div>
        </header>

        {currentUser && currentUser.role !== '系统管理员' && (
          <section className="panel">
            <div className="section-title">
              <div>
                <h2>订阅状态</h2>
                <span>{currentUser.can_use_core_features ? '当前可正常使用核心功能' : '当前仅可查看数据，需先激活或续费'}</span>
              </div>
            </div>
            <div className="summary-grid">
              <article className="summary-card">
                <span>当前状态</span>
                <strong>{currentUser.subscription_status}</strong>
                <p>到期时间 {currentUser.subscription_expires_at ?? '未激活'}</p>
              </article>
              <article className="summary-card">
                <span>卡密兑换</span>
                <strong>月卡 / 年卡</strong>
                <p><input value={redeemCode} onChange={(event) => setRedeemCode(event.target.value)} placeholder="请输入卡密" /></p>
                <button className="primary" onClick={submitRedeemCode}>兑换</button>
              </article>
            </div>
          </section>
        )}

        {runtime && currentUser?.role === '系统管理员' && activeView === 'developerApps' && (
          <section className="runtime-strip">
            <span>系统诊断</span>
            <span>任务通道：{runtime.queue_backend}</span>
            <span>TG 连接：{runtime.telethon_configured ? '已配置' : '待配置'}</span>
            <span>应用池：{runtime.developer_app_healthy_count}/{runtime.developer_app_count} 正常</span>
            <span>AI 服务：{runtime.healthy_ai_provider_count}/{runtime.ai_provider_count} 正常{runtime.mock_ai_fallback_enabled ? ' / 可回退' : ''}</span>
          </section>
        )}

        {notice && (
          <section className="notice">
            <CheckCircle2 size={18} />
            <span>{notice}</span>
            <button onClick={() => setNotice('')}>关闭</button>
          </section>
        )}

        {/* ===== View routing ===== */}
        {activeView === 'overview' && overview && <OverviewView overview={overview} runtime={runtime} />}
        {activeView === 'developerApps' && (
          <DeveloperAppsView developerApps={developerApps} tenants={tenants} onCreateClick={() => setModal({ type: 'developerAppCreate' })} onCheck={checkDeveloperApp} onToggle={toggleDeveloperApp} onEditTenant={openTenantEdit} onOpenConfirm={openConfirm} />
        )}
        {activeView === 'aiSettings' && (
          <AISettingsView aiProviders={aiProviders} promptTemplates={promptTemplates} tenantAiSetting={tenantAiSetting} schedulingSetting={schedulingSetting} materials={materials} currentUserRole={currentUser?.role} onCreateProvider={() => setModal({ type: 'aiProviderCreate' })} onCheckProvider={checkAiProvider} onEditTenantAi={() => setModal({ type: 'tenantAiEdit' })} onEditScheduling={() => setModal({ type: 'schedulingEdit' })} onCreatePromptTemplate={() => setModal({ type: 'promptTemplateCreate' })} onCreateMaterial={() => setModal({ type: 'materialCreate' })} />
        )}
        {activeView === 'activationCodes' && (
          <ActivationCodesView activationCodes={activationCodes} activationBatch={activationBatch} setActivationBatch={setActivationBatch} onCreateCodes={createActivationCodes} />
        )}
        {activeView === 'usageReports' && <UsageReportsView usageLedgers={usageLedgers} usageSummary={usageSummary} />}
        {activeView === 'accounts' && (
          <AccountsView accounts={accounts} accountPools={accountPools} selectedPoolId={selectedPoolId} setSelectedPoolId={setSelectedPoolId} selectedPool={selectedPool ?? undefined} avatarUrl={avatarUrl} onCreatePoolClick={() => setModal({ type: 'accountPoolCreate' })} onCreateAccount={openAccountCreate} onOpenPoolDetail={openAccountPoolDetail} onOpenAccountDetail={openAccountDetail} onRunLogin={runLogin} onVerifyAccount={verifyAccount} onHealthCheck={healthCheck} onSyncGroups={syncAccountGroups} />
        )}
        {activeView === 'groups' && (
          <GroupsView groups={groups} selectedGroup={selectedGroup ?? undefined} selectedGroupId={selectedGroupId} setSelectedGroupId={setSelectedGroupId} onCreateCampaign={openCampaignModal} onCreateArchive={createArchive} onAuthorizeGroup={authorizeSelectedGroup} onEditGroupPolicy={() => setModal({ type: 'groupPolicyEdit' })} onOpenConfirm={openConfirm} />
        )}
        {activeView === 'taskManagement' && (
          <CampaignsView campaigns={campaigns} tasks={tasks} drafts={drafts} groups={groups} accounts={accounts} taskManagementTab={taskManagementTab} setTaskManagementTab={setTaskManagementTab} taskSummary={taskSummary} selectedCampaign={selectedCampaign ?? undefined} selectedCampaignDrafts={selectedCampaignDrafts} selectedCampaignTasks={selectedCampaignTasks} taskStatusFilter={taskStatusFilter} setTaskStatusFilter={setTaskStatusFilter} setSelectedCampaignId={setSelectedCampaignId} onCreateCampaign={() => openCampaignModal()} onApproveDraft={approveDraft} onApproveAllDrafts={approveAllDrafts} onDispatchTask={dispatchTask} onRetryTask={retryTask} onDrainQueue={drainQueue} onOpenConfirm={openConfirm} groupName={groupName} accountName={accountName} />
        )}
        {activeView === 'archives' && <ArchivesView archives={archives} archiveDetail={archiveDetail} onOpenArchiveDetail={openArchiveDetail} />}
        {activeView === 'audits' && <AuditsView audits={audits} />}

        {/* ===== Modals ===== */}
        <AppModals />
      </main>
    </div>
  );
}

export default function App() {
  return (
    <AppProvider>
      <AppShell />
    </AppProvider>
  );
}
