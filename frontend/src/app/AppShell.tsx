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
import { Modal, FormActions, ConfirmDialog, ResultDialog, StatusBadge } from './components/shared';
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
import CampaignWizard from './views/CampaignWizard';
import { AccountPoolDetailModal, AccountDetailModal } from './views/AccountModals';
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
        {modal?.type === 'developerAppCreate' && (
          <Modal title="新增开发者应用" size="medium" onClose={closeModal}>
            <div className="policy-grid">
              <label>应用名称<input value={developerAppForm.app_name} onChange={(event) => setDeveloperAppForm({ ...developerAppForm, app_name: event.target.value })} /></label>
              <label>API ID<input value={developerAppForm.api_id} onChange={(event) => setDeveloperAppForm({ ...developerAppForm, api_id: event.target.value })} /></label>
              <label>账号上限<input type="number" min={0} value={developerAppForm.max_accounts} onChange={(event) => setDeveloperAppForm({ ...developerAppForm, max_accounts: Number(event.target.value) })} /></label>
              <label>备注<input value={developerAppForm.notes} onChange={(event) => setDeveloperAppForm({ ...developerAppForm, notes: event.target.value })} /></label>
              <label className="wide-field">API Hash<input type="password" value={developerAppForm.api_hash} onChange={(event) => setDeveloperAppForm({ ...developerAppForm, api_hash: event.target.value })} /></label>
            </div>
            <FormActions submitLabel="新增应用" onCancel={closeModal} onSubmit={createDeveloperApp} disabled={!developerAppForm.api_id || developerAppForm.api_hash.length < 8} />
          </Modal>
        )}

        {modal?.type === 'aiProviderCreate' && (
          <Modal title="新增 AI 供应商" size="medium" onClose={closeModal}>
            <div className="policy-grid">
              <label>名称<input value={aiProviderForm.provider_name} onChange={(event) => setAiProviderForm({ ...aiProviderForm, provider_name: event.target.value })} /></label>
              <label>Base URL<input value={aiProviderForm.base_url} onChange={(event) => setAiProviderForm({ ...aiProviderForm, base_url: event.target.value })} /></label>
              <label>模型名<input value={aiProviderForm.model_name} onChange={(event) => setAiProviderForm({ ...aiProviderForm, model_name: event.target.value })} /></label>
              <label>Key Header<input value={aiProviderForm.api_key_header} onChange={(event) => setAiProviderForm({ ...aiProviderForm, api_key_header: event.target.value })} /></label>
              <label className="wide-field">API Key<input type="password" value={aiProviderForm.api_key} onChange={(event) => setAiProviderForm({ ...aiProviderForm, api_key: event.target.value })} /></label>
              <label className="wide-field">备注<input value={aiProviderForm.notes} onChange={(event) => setAiProviderForm({ ...aiProviderForm, notes: event.target.value })} /></label>
            </div>
            <FormActions submitLabel="新增供应商" onCancel={closeModal} onSubmit={createAiProvider} disabled={aiProviderForm.api_key.length < 4} />
          </Modal>
        )}

        {modal?.type === 'tenantEdit' && (
          <Modal title="编辑租户配额" size="medium" onClose={closeModal}>
            <div className="policy-grid">
              <label>租户名称<input value={tenantForm.name} onChange={(event) => setTenantForm({ ...tenantForm, name: event.target.value })} /></label>
              <label>套餐名称<input value={tenantForm.plan_name} onChange={(event) => setTenantForm({ ...tenantForm, plan_name: event.target.value })} /></label>
              <label>账号配额<input type="number" min={0} value={tenantForm.account_quota} onChange={(event) => setTenantForm({ ...tenantForm, account_quota: Number(event.target.value) })} /></label>
              <label>任务配额<input type="number" min={0} value={tenantForm.task_quota} onChange={(event) => setTenantForm({ ...tenantForm, task_quota: Number(event.target.value) })} /></label>
            </div>
            <FormActions submitLabel="保存配额" onCancel={closeModal} onSubmit={saveTenantQuota} disabled={!tenantForm.name || !tenantForm.plan_name} />
          </Modal>
        )}

        {modal?.type === 'tenantAiEdit' && tenantAiSetting && (
          <Modal title="编辑客户 AI 配置" size="medium" onClose={closeModal}>
            <div className="policy-grid">
              <label>默认模型
                <select value={selectedAiProviderId} onChange={(event) => setSelectedAiProviderId(Number(event.target.value))}>
                  {aiProviders.map((provider) => <option key={provider.id} value={provider.id}>{provider.provider_name} / {provider.model_name}</option>)}
                </select>
              </label>
              <label>温度<input type="number" min={0} max={2} step={0.1} value={tenantAiSetting.temperature} onChange={(event) => setTenantAiSetting({ ...tenantAiSetting, temperature: Number(event.target.value) })} /></label>
              <label>最大 Token<input type="number" min={128} max={8192} value={tenantAiSetting.max_tokens} onChange={(event) => setTenantAiSetting({ ...tenantAiSetting, max_tokens: Number(event.target.value) })} /></label>
              <label className="checkbox-line"><input type="checkbox" checked={tenantAiSetting.ai_enabled} onChange={(event) => setTenantAiSetting({ ...tenantAiSetting, ai_enabled: event.target.checked })} />启用 AI 草稿</label>
              <label className="checkbox-line"><input type="checkbox" checked={tenantAiSetting.fallback_to_mock} onChange={(event) => setTenantAiSetting({ ...tenantAiSetting, fallback_to_mock: event.target.checked })} />失败回退模板</label>
            </div>
            <FormActions onCancel={closeModal} onSubmit={saveTenantAiSetting} />
          </Modal>
        )}

        {modal?.type === 'schedulingEdit' && (
          <Modal title="编辑发送节奏" size="medium" onClose={closeModal}>
            <div className="policy-grid">
              <label>最小抖动秒<input type="number" min={0} value={jitterMinSeconds} onChange={(event) => setJitterMinSeconds(Number(event.target.value))} /></label>
              <label>最大抖动秒<input type="number" min={0} value={jitterMaxSeconds} onChange={(event) => setJitterMaxSeconds(Number(event.target.value))} /></label>
              <label>批次间隔秒<input type="number" min={0} value={batchIntervalSeconds} onChange={(event) => setBatchIntervalSeconds(Number(event.target.value))} /></label>
              <label className="checkbox-line"><input type="checkbox" checked={respectSendWindow} onChange={(event) => setRespectSendWindow(event.target.checked)} />遵守发送时间窗</label>
            </div>
            <FormActions onCancel={closeModal} onSubmit={saveSchedulingSetting} />
          </Modal>
        )}

        {modal?.type === 'promptTemplateCreate' && (
          <Modal title="新增提示词模板" size="large" onClose={closeModal}>
            <div className="policy-grid">
              <label>模板名称<input value={promptTemplateForm.name} onChange={(event) => setPromptTemplateForm({ ...promptTemplateForm, name: event.target.value })} /></label>
              <label>模板类型
                <select value={promptTemplateForm.template_type} onChange={(event) => setPromptTemplateForm({ ...promptTemplateForm, template_type: event.target.value })}>
                  <option>系统决策提示词</option><option>群活跃草稿</option><option>多账号对话脚本</option><option>素材配文</option><option>风险检查</option>
                </select>
              </label>
              <label className="wide-field">模板内容<textarea value={promptTemplateForm.content} onChange={(event) => setPromptTemplateForm({ ...promptTemplateForm, content: event.target.value })} /></label>
            </div>
            <FormActions submitLabel="新增提示词" onCancel={closeModal} onSubmit={createPromptTemplate} disabled={!promptTemplateForm.name || !promptTemplateForm.content} />
          </Modal>
        )}

        {modal?.type === 'materialCreate' && (
          <Modal title="新增素材" size="medium" onClose={closeModal}>
            <div className="policy-grid">
              <label>素材标题<input value={materialForm.title} onChange={(event) => setMaterialForm({ ...materialForm, title: event.target.value })} /></label>
              <label>素材类型
                <select value={materialForm.material_type} onChange={(event) => setMaterialForm({ ...materialForm, material_type: event.target.value })}>
                  <option>文本</option><option>图片</option><option>表情包</option><option>文件</option><option>链接</option><option>组合消息</option>
                </select>
              </label>
              <label>标签<input value={materialForm.tags} onChange={(event) => setMaterialForm({ ...materialForm, tags: event.target.value })} /></label>
              <label className="wide-field">内容/URL<textarea value={materialForm.content} onChange={(event) => setMaterialForm({ ...materialForm, content: event.target.value })} /></label>
            </div>
            <FormActions submitLabel="新增素材" onCancel={closeModal} onSubmit={createMaterial} disabled={!materialForm.title || !materialForm.content} />
          </Modal>
        )}

        {modal?.type === 'accountPoolDetail' && accountPoolDetail && (
          <AccountPoolDetailModal accountPoolDetail={accountPoolDetail} poolDirectAccountId={poolDirectAccountId} setPoolDirectAccountId={setPoolDirectAccountId} directMessageForm={directMessageForm} setDirectMessageForm={setDirectMessageForm} selectedDirectContact={selectedDirectContact} onClose={closeModal} onOpenAccountCreate={openAccountCreate} onOpenAccountDetail={openAccountDetail} onRefreshAccountPoolDetail={refreshAccountPoolDetail} onStartDirectMessageToContact={startDirectMessageToContact} onCreateDirectMessageTask={createDirectMessageTask} onOpenConfirm={openConfirm} onSetReturnAfterVerification={setReturnAfterVerification} onSetModal={setModal} accountName={accountName} />
        )}

        {modal?.type === 'accountPoolCreate' && (
          <Modal title="新增账号池" size="medium" onClose={closeModal}>
            <div className="policy-grid">
              <label>账号池名称<input value={accountPoolForm.name} onChange={(event) => setAccountPoolForm({ ...accountPoolForm, name: event.target.value })} /></label>
              <label className="checkbox-line"><input type="checkbox" checked={accountPoolForm.is_default} onChange={(event) => setAccountPoolForm({ ...accountPoolForm, is_default: event.target.checked })} />设为默认账号池</label>
              <label className="wide-field">说明<textarea value={accountPoolForm.description} onChange={(event) => setAccountPoolForm({ ...accountPoolForm, description: event.target.value })} /></label>
            </div>
            <FormActions submitLabel="新增账号池" onCancel={closeModal} onSubmit={createAccountPool} disabled={!accountPoolForm.name.trim()} />
          </Modal>
        )}

        {modal?.type === 'accountCreate' && (
          <Modal title={loginAfterCreate ? '新增登录账号' : '新增账号'} size="medium" onClose={closeModal}>
            <div className="policy-grid">
              <label>所属账号池
                <select value={accountCreateForm.pool_id} onChange={(event) => setAccountCreateForm({ ...accountCreateForm, pool_id: Number(event.target.value) || '' })}>
                  <option value="">默认账号池</option>
                  {accountPools.map((pool) => <option key={pool.id} value={pool.id}>{pool.name}</option>)}
                </select>
              </label>
              <label>平台备注名<input value={accountCreateForm.display_name} onChange={(event) => setAccountCreateForm({ ...accountCreateForm, display_name: event.target.value })} /></label>
              <label>TG 用户名<input value={accountCreateForm.username} onChange={(event) => setAccountCreateForm({ ...accountCreateForm, username: event.target.value })} placeholder="可选，不含 @" /></label>
              <label className="wide-field">手机号<input value={accountCreateForm.phone_number} onChange={(event) => setAccountCreateForm({ ...accountCreateForm, phone_number: event.target.value })} placeholder="+8613800000000" /></label>
            </div>
            <p className="muted-line">{loginAfterCreate ? '创建后会直接启动扫码登录流程，也可以在账号详情切换验证码登录。' : '创建后会进入账号详情，后续可选择扫码或验证码登录。'}</p>
            <FormActions submitLabel={loginAfterCreate ? '创建并登录' : '添加账号'} onCancel={closeModal} onSubmit={createAccount} disabled={!accountCreateForm.display_name.trim() || !accountCreateForm.phone_number.trim()} />
          </Modal>
        )}

        {modal?.type === 'accountMovePool' && accountDetail && (
          <Modal title="移动账号池" size="small" onClose={() => setModal({ type: 'accountDetail' })}>
            <div className="policy-grid">
              <label>目标账号池
                <select value={accountDetail.account.pool_id ?? ''} onChange={(event) => moveCurrentAccountPool(Number(event.target.value))}>
                  {accountPools.map((pool) => <option key={pool.id} value={pool.id}>{pool.name}</option>)}
                </select>
              </label>
            </div>
            <div className="modal-actions"><button onClick={() => setModal({ type: 'accountDetail' })}>返回</button></div>
          </Modal>
        )}

        {modal?.type === 'accountCloneCreate' && accountDetail && (
          <Modal title="创建账号克隆计划" size="medium" onClose={() => setModal({ type: 'accountDetail' })}>
            <div className="target-account-grid">
              {accounts.filter((account) => account.id !== accountDetail.account.id).map((account) => {
                const selected = cloneForm.target_account_ids.includes(account.id);
                return (
                  <button key={account.id} type="button" className={selected ? 'selected contact-pick' : 'contact-pick'} onClick={() => setCloneForm({ ...cloneForm, target_account_ids: selected ? cloneForm.target_account_ids.filter((id) => id !== account.id) : [...cloneForm.target_account_ids, account.id] })}>
                    <strong>{account.display_name}</strong><span>{account.pool_name}</span><StatusBadge status={account.status} />
                  </button>
                );
              })}
            </div>
            <div className="policy-grid">
              <label className="checkbox-line"><input type="checkbox" checked={cloneForm.clone_contacts} onChange={(event) => setCloneForm({ ...cloneForm, clone_contacts: event.target.checked })} />克隆好友和私聊对象</label>
              <label className="checkbox-line"><input type="checkbox" checked={cloneForm.clone_groups} onChange={(event) => setCloneForm({ ...cloneForm, clone_groups: event.target.checked })} />克隆群聊和频道清单</label>
            </div>
            <p className="muted-line">已选择 {cloneForm.target_account_ids.length} 个目标账号。系统会先生成计划，确认后逐项执行。</p>
            <FormActions submitLabel="生成克隆计划" onCancel={() => setModal({ type: 'accountDetail' })} onSubmit={createClonePlan} disabled={!cloneForm.target_account_ids.length || (!cloneForm.clone_contacts && !cloneForm.clone_groups)} />
          </Modal>
        )}

        {modal?.type === 'verificationTaskDetail' && (
          <Modal title="验证辅助处理" size="medium" onClose={() => setModal({ type: returnAfterVerification })}>
            <div className="detail-list">
              <div><dt>状态</dt><dd><StatusBadge status={modal.payload.status} /></dd></div>
              <div><dt>验证类型</dt><dd>{modal.payload.verification_type}</dd></div>
              <div><dt>建议操作</dt><dd>{modal.payload.suggested_action}</dd></div>
              <div><dt>目标</dt><dd>{modal.payload.target_display || modal.payload.target_peer_id || '当前群聊'}</dd></div>
            </div>
            <p className="dialog-message">{modal.payload.detected_reason || '平台检测到当前账号在该群可能需要完成验证后才能发言。'}</p>
            <p className="muted-line">平台只会在你确认后执行可控动作。</p>
            <div className="modal-actions">
              <button onClick={() => setModal({ type: returnAfterVerification })}>返回</button>
              <button onClick={() => dismissVerificationTask(modal.payload)}>忽略</button>
              <button className="primary" disabled={!['待处理', '失败'].includes(modal.payload.status)} onClick={() => confirmVerificationTask(modal.payload)}>确认处理</button>
            </div>
          </Modal>
        )}

        {modal?.type === 'groupPolicyEdit' && selectedGroup && (
          <Modal title="编辑群运营配置" size="large" onClose={closeModal}>
            <div className="policy-grid">
              <label>活跃时间<input value={groupPolicy.active_window} onChange={(event) => setGroupPolicy({ ...groupPolicy, active_window: event.target.value })} /></label>
              <label>每日上限<input type="number" value={groupPolicy.daily_limit} onChange={(event) => setGroupPolicy({ ...groupPolicy, daily_limit: Number(event.target.value) })} /></label>
              <label>账号冷却秒<input type="number" value={groupPolicy.account_cooldown_seconds} onChange={(event) => setGroupPolicy({ ...groupPolicy, account_cooldown_seconds: Number(event.target.value) })} /></label>
              <label>群冷却秒<input type="number" value={groupPolicy.group_cooldown_seconds} onChange={(event) => setGroupPolicy({ ...groupPolicy, group_cooldown_seconds: Number(event.target.value) })} /></label>
              <label>话题方向<textarea value={groupPolicy.topic_direction} onChange={(event) => setGroupPolicy({ ...groupPolicy, topic_direction: event.target.value })} /></label>
              <label>禁用词<textarea value={groupPolicy.banned_words} onChange={(event) => setGroupPolicy({ ...groupPolicy, banned_words: event.target.value })} /></label>
              <label>链接白名单<textarea value={groupPolicy.link_whitelist} onChange={(event) => setGroupPolicy({ ...groupPolicy, link_whitelist: event.target.value })} /></label>
              <label className="checkbox-line"><input type="checkbox" checked={groupPolicy.require_review} onChange={(event) => setGroupPolicy({ ...groupPolicy, require_review: event.target.checked })} />需要人工审核</label>
            </div>
            <FormActions onCancel={closeModal} onSubmit={saveGroupPolicy} />
          </Modal>
        )}

        {modal?.type === 'accountProfileEdit' && accountDetail && (
          <Modal title="编辑账号资料" size="medium" onClose={() => setModal({ type: 'accountDetail' })}>
            <div className="profile-edit-layout">
              <div className="avatar-preview">
                {avatarFile ? <img src={URL.createObjectURL(avatarFile)} alt="" /> : profileForm.avatar_object_key ? <img src={avatarUrl(`/media/${profileForm.avatar_object_key}`)} alt="" /> : <span>{profileForm.display_name.slice(0, 1) || 'T'}</span>}
              </div>
              <div className="policy-grid">
                <label>平台备注名<input value={profileForm.display_name} onChange={(event) => setProfileForm({ ...profileForm, display_name: event.target.value })} /></label>
                <label>TG 名<input value={profileForm.tg_first_name} onChange={(event) => setProfileForm({ ...profileForm, tg_first_name: event.target.value })} /></label>
                <label>TG 姓<input value={profileForm.tg_last_name} onChange={(event) => setProfileForm({ ...profileForm, tg_last_name: event.target.value })} /></label>
                <label className="wide-field">头像上传<input type="file" accept={runtime?.avatar_allowed_types.join(',') ?? 'image/jpeg,image/png,image/webp'} onChange={(event) => setAvatarFile(event.target.files?.[0] ?? null)} /></label>
                <label className="wide-field">TG 简介<textarea value={profileForm.tg_bio} maxLength={220} onChange={(event) => setProfileForm({ ...profileForm, tg_bio: event.target.value })} /></label>
              </div>
            </div>
            <p className="muted-line">头像最大 {Math.round((runtime?.avatar_max_bytes ?? 0) / 1024 / 1024) || 2}MB；保存后会自动进入后台同步处理。</p>
            <FormActions submitLabel="保存并同步" onCancel={() => setModal({ type: 'accountDetail' })} onSubmit={saveAccountProfile} disabled={!profileForm.display_name.trim()} />
          </Modal>
        )}

        {modal?.type === 'draftEdit' && draftEditTarget && (
          <Modal title="编辑草稿" size="medium" onClose={closeModal}>
            <div className="policy-grid">
              <label>风险等级
                <select value={draftEditForm.risk_level} onChange={(event) => setDraftEditForm({ ...draftEditForm, risk_level: event.target.value })}>
                  <option>低</option><option>中</option><option>高</option>
                </select>
              </label>
              <label>建议账号
                <select value={draftEditForm.suggested_account_id} onChange={(event) => setDraftEditForm({ ...draftEditForm, suggested_account_id: Number(event.target.value) || '' })}>
                  <option value="">不指定</option>
                  {accounts.map((account) => <option key={account.id} value={account.id}>{account.display_name}</option>)}
                </select>
              </label>
              <label className="wide-field">草稿内容<textarea value={draftEditForm.content} onChange={(event) => setDraftEditForm({ ...draftEditForm, content: event.target.value })} /></label>
            </div>
            <FormActions onCancel={closeModal} onSubmit={saveDraftEdit} disabled={!draftEditForm.content.trim()} />
          </Modal>
        )}

        {modal?.type === 'accountDetail' && accountDetail && (
          <AccountDetailModal accountDetail={accountDetail} accountDetailTab={accountDetailTab} setAccountDetailTab={setAccountDetailTab} runtime={runtime} directMessageForm={directMessageForm} setDirectMessageForm={setDirectMessageForm} selectedDirectContact={selectedDirectContact} accountContacts={accountContacts} accounts={accounts} avatarUrl={avatarUrl} onClose={closeModal} onOpenAccountProfileEdit={openAccountProfileEdit} onQueueAccountSyncNow={queueAccountSyncNow} onPollVerificationCodes={pollVerificationCodes} onStartDirectMessageToContact={startDirectMessageToContact} onCreateDirectMessageTask={createDirectMessageTask} onConfirmClonePlan={confirmClonePlan} onRetryCloneItem={retryCloneItem} onRetryAccountProfileSync={retryAccountProfileSync} onDismissVerificationTask={dismissVerificationTask} onConfirmVerificationTask={confirmVerificationTask} onOpenConfirm={openConfirm} onSetReturnAfterVerification={setReturnAfterVerification} onSetModal={setModal} onSetCloneForm={setCloneForm} accountName={accountName} />
        )}

        {modal?.type === 'campaignCreate' && (
          <CampaignWizard groups={groups} aiProviders={aiProviders} materials={materials} campaignStep={campaignStep} setCampaignStep={setCampaignStep} selectedTargetGroupIds={selectedTargetGroupIds} recommendedAccounts={recommendedAccounts} selectedAccountsByGroup={selectedAccountsByGroup} targetGroupsMissingAccounts={targetGroupsMissingAccounts} topic={topic} setTopic={setTopic} sendWindow={sendWindow} setSendWindow={setSendWindow} intensity={intensity} setIntensity={setIntensity} draftCount={draftCount} setDraftCount={setDraftCount} tone={tone} setTone={setTone} selectedAiProviderId={selectedAiProviderId} setSelectedAiProviderId={setSelectedAiProviderId} selectedMaterialIds={selectedMaterialIds} jitterMinSeconds={jitterMinSeconds} setJitterMinSeconds={setJitterMinSeconds} jitterMaxSeconds={jitterMaxSeconds} setJitterMaxSeconds={setJitterMaxSeconds} batchIntervalSeconds={batchIntervalSeconds} setBatchIntervalSeconds={setBatchIntervalSeconds} respectSendWindow={respectSendWindow} setRespectSendWindow={setRespectSendWindow} onClose={closeModal} onToggleTargetGroup={toggleTargetGroup} onGoAccountStep={goCampaignAccountStep} onGoContentStep={goCampaignContentStep} onToggleRecommendedAccount={toggleRecommendedAccount} onSetGroupAccountsSelected={setGroupAccountsSelected} onToggleMaterial={toggleMaterial} onCreateCampaignAndDrafts={createCampaignAndDrafts} groupName={groupName} />
        )}

        {modal?.type === 'confirmAction' && <ConfirmDialog payload={modal.payload} onClose={closeModal} />}
        {resultDialog && <ResultDialog dialog={resultDialog} onClose={() => setResultDialog(null)} />}
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
