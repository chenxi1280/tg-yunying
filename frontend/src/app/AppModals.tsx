import { useAppContext } from './context';
import { Button, Checkbox, Input, InputNumber, Select, Space } from 'antd';
import { Modal, FormActions, ConfirmDialog, ResultDialog, StatusBadge } from './components/shared';
import { AccountDetailModal, AccountPoolDetailModal } from './views/AccountModals';
import CampaignWizard from './views/CampaignWizard';

/** 从 AppShell 中提取的所有模态框渲染组件。 */
export function AppModals() {
  const ctx = useAppContext();
  const {
    modal, closeModal, resultDialog, setResultDialog,
    changePasswordForm, setChangePasswordForm, changePassword,
    // Developer App
    developerAppForm, setDeveloperAppForm, createDeveloperApp,
    // AI Provider
    aiProviderForm, setAiProviderForm, createAiProvider,
    // Tenant
    tenantForm, setTenantForm, saveTenantQuota,
    subscriptionPlanForm, setSubscriptionPlanForm, createSubscriptionPlan,
    adminUserForm, setAdminUserForm, saveAdminUser, tokenAdjustmentForm, setTokenAdjustmentForm, adminUsers, selectedUserTokenLedgers, adjustAdminUserTokens, resetAdminUserPassword,
    // Tenant AI
    tenantAiSetting, setTenantAiSetting, selectedAiProviderId, setSelectedAiProviderId, aiProviders, saveTenantAiSetting,
    // Scheduling
    jitterMinSeconds, setJitterMinSeconds, jitterMaxSeconds, setJitterMaxSeconds, batchIntervalSeconds, setBatchIntervalSeconds, respectSendWindow, setRespectSendWindow, saveSchedulingSetting,
    // Prompt Template
    promptTemplateForm, setPromptTemplateForm, createPromptTemplate,
    // Material
    materialForm, setMaterialForm, createMaterial, keywordRuleForm, setKeywordRuleForm, createContentKeywordRule, saveContentKeywordRule,
    // Account Pool
    accountPoolForm, setAccountPoolForm, createAccountPool, accountPoolDetail, poolDirectAccountId, setPoolDirectAccountId, refreshAccountPoolDetail,
    // Account
    accountCreateForm, setAccountCreateForm, createAccount, loginAfterCreate, accountLoginForm, setAccountLoginForm, submitAccountLoginCode, submitAccountLoginPassword, resendAccountLoginCode, checkAccountQrLogin, accountPools, accounts, accountDetail, setAccountDetailTab, accountDetailTab, runtime, cloneForm, setCloneForm, createClonePlan, confirmClonePlan, moveCurrentAccountPool,
    // Profile
    profileForm, setProfileForm, avatarFile, setAvatarFile, avatarUrl, saveAccountProfile, openAccountProfileEdit, queueAccountSyncNow, pollVerificationCodes, retryAccountProfileSync,
    // Group
    selectedGroup, groupPolicy, setGroupPolicy, saveGroupPolicy, groupDetail,
    // Campaign
    groups, campaigns, materials, campaignStep, setCampaignStep, campaignMode, setCampaignMode, selectedTargetGroupIds, selectedSourceGroupIds, recommendedAccounts, selectedAccountsByGroup, targetGroupsMissingAccounts, topic, setTopic, sendWindow, setSendWindow, intensity, setIntensity, draftCount, setDraftCount, tone, setTone, selectedMaterialIds, toggleTargetGroup, toggleSourceGroup, goCampaignAccountStep, goCampaignContentStep, toggleRecommendedAccount, setGroupAccountsSelected, toggleMaterial, createCampaignAndDrafts, selectedCampaignId, setSelectedCampaignId, campaignEndsAt, setCampaignEndsAt, maxAiTokens, setMaxAiTokens, runIntervalSeconds, setRunIntervalSeconds, participationMinRatio, setParticipationMinRatio, participationMaxRatio, setParticipationMaxRatio, maxMessagesPerAccount, setMaxMessagesPerAccount, maxDraftsPerBatch, setMaxDraftsPerBatch,
    // Draft
    draftEditTarget, draftEditForm, setDraftEditForm, saveDraftEdit,
    // Verification
    returnAfterVerification, setReturnAfterVerification, confirmVerificationTask, dismissVerificationTask,
    // Direct Message
    directMessageForm, setDirectMessageForm, selectedDirectContact, startDirectMessageToContact, createDirectMessageTask,
    // Misc
    openAccountCreate, openAccountDetail, openConfirm, accountContacts, accountName, groupName, busy,
  } = ctx;

  if (!modal && !resultDialog) return null;

  return (
    <>
      {(modal?.type === 'developerAppCreate' || modal?.type === 'developerAppEdit') && (
        <Modal title={modal.type === 'developerAppEdit' ? '编辑开发者应用' : '新增开发者应用'} size="medium" onClose={closeModal}>
          <div className="policy-grid">
            <label>应用名称<Input value={developerAppForm.app_name} onChange={(event) => setDeveloperAppForm({ ...developerAppForm, app_name: event.target.value })} /></label>
            <label>API ID<Input value={developerAppForm.api_id} onChange={(event) => setDeveloperAppForm({ ...developerAppForm, api_id: event.target.value })} /></label>
            <label>账号上限<InputNumber min={0} value={developerAppForm.max_accounts} onChange={(value) => setDeveloperAppForm({ ...developerAppForm, max_accounts: Number(value ?? 0) })} /></label>
            <label>备注<Input value={developerAppForm.notes} onChange={(event) => setDeveloperAppForm({ ...developerAppForm, notes: event.target.value })} /></label>
            <label className="wide-field">API Hash<Input.Password value={developerAppForm.api_hash} onChange={(event) => setDeveloperAppForm({ ...developerAppForm, api_hash: event.target.value })} placeholder={modal.type === 'developerAppEdit' ? '不填写则保留原凭证' : ''} /></label>
            <Checkbox checked={developerAppForm.is_active} onChange={(event) => setDeveloperAppForm({ ...developerAppForm, is_active: event.target.checked })}>启用应用</Checkbox>
          </div>
          <FormActions submitLabel={modal.type === 'developerAppEdit' ? '保存应用' : '新增应用'} onCancel={closeModal} onSubmit={createDeveloperApp} disabled={!developerAppForm.app_name.trim() || !developerAppForm.api_id || (modal.type === 'developerAppCreate' && developerAppForm.api_hash.length < 8)} />
        </Modal>
      )}

      {(modal?.type === 'aiProviderCreate' || modal?.type === 'aiProviderEdit') && (
        <Modal title={modal.type === 'aiProviderEdit' ? '编辑 AI 供应商' : '新增 AI 供应商'} size="medium" onClose={closeModal}>
          <div className="policy-grid">
            <label>名称<Input value={aiProviderForm.provider_name} onChange={(event) => setAiProviderForm({ ...aiProviderForm, provider_name: event.target.value })} /></label>
            <label>Base URL<Input value={aiProviderForm.base_url} onChange={(event) => setAiProviderForm({ ...aiProviderForm, base_url: event.target.value })} /></label>
            <label>模型名<Input value={aiProviderForm.model_name} onChange={(event) => setAiProviderForm({ ...aiProviderForm, model_name: event.target.value })} /></label>
            <label>Key Header<Input value={aiProviderForm.api_key_header} onChange={(event) => setAiProviderForm({ ...aiProviderForm, api_key_header: event.target.value })} /></label>
            <label className="wide-field">API Key<Input.Password value={aiProviderForm.api_key} onChange={(event) => setAiProviderForm({ ...aiProviderForm, api_key: event.target.value })} placeholder={modal.type === 'aiProviderEdit' ? '不填写则保留原 Key' : ''} /></label>
            <label className="wide-field">备注<Input value={aiProviderForm.notes} onChange={(event) => setAiProviderForm({ ...aiProviderForm, notes: event.target.value })} /></label>
            <Checkbox checked={aiProviderForm.is_active} onChange={(event) => setAiProviderForm({ ...aiProviderForm, is_active: event.target.checked })}>启用供应商</Checkbox>
          </div>
          <FormActions submitLabel={modal.type === 'aiProviderEdit' ? '保存供应商' : '新增供应商'} onCancel={closeModal} onSubmit={createAiProvider} disabled={!aiProviderForm.provider_name.trim() || !aiProviderForm.base_url.trim() || !aiProviderForm.model_name.trim() || (modal.type === 'aiProviderCreate' && aiProviderForm.api_key.length < 4)} />
        </Modal>
      )}

      {modal?.type === 'tenantEdit' && (
        <Modal title="编辑租户配额" size="medium" onClose={closeModal}>
          <div className="policy-grid">
            <label>租户名称<Input value={tenantForm.name} onChange={(event) => setTenantForm({ ...tenantForm, name: event.target.value })} /></label>
            <label>套餐名称<Input value={tenantForm.plan_name} onChange={(event) => setTenantForm({ ...tenantForm, plan_name: event.target.value })} /></label>
            <label>账号配额<InputNumber min={0} value={tenantForm.account_quota} onChange={(value) => setTenantForm({ ...tenantForm, account_quota: Number(value ?? 0) })} /></label>
            <label>任务配额<InputNumber min={0} value={tenantForm.task_quota} onChange={(value) => setTenantForm({ ...tenantForm, task_quota: Number(value ?? 0) })} /></label>
          </div>
          <FormActions submitLabel="保存配额" onCancel={closeModal} onSubmit={saveTenantQuota} disabled={!tenantForm.name || !tenantForm.plan_name} />
        </Modal>
      )}

      {(modal?.type === 'subscriptionPlanCreate' || modal?.type === 'subscriptionPlanEdit') && (
        <Modal title={modal.type === 'subscriptionPlanEdit' ? '编辑套餐' : '新增套餐'} size="medium" onClose={closeModal}>
          <div className="policy-grid">
            <label>套餐标识<Input disabled={modal.type === 'subscriptionPlanEdit'} value={subscriptionPlanForm.plan_type} onChange={(event) => setSubscriptionPlanForm((current) => ({ ...current, plan_type: event.target.value.trim().toLowerCase() }))} placeholder="monthly" /></label>
            <label>套餐名称<Input value={subscriptionPlanForm.name} onChange={(event) => setSubscriptionPlanForm((current) => ({ ...current, name: event.target.value }))} /></label>
            <label>有效天数<InputNumber min={1} value={subscriptionPlanForm.duration_days} onChange={(value) => setSubscriptionPlanForm((current) => ({ ...current, duration_days: Number(value ?? 1) }))} /></label>
            <label>赠送 Token<InputNumber min={0} value={subscriptionPlanForm.token_quota} onChange={(value) => setSubscriptionPlanForm((current) => ({ ...current, token_quota: Number(value ?? 0) }))} /></label>
            <label className="wide-field">备注<Input value={subscriptionPlanForm.note} onChange={(event) => setSubscriptionPlanForm((current) => ({ ...current, note: event.target.value }))} /></label>
            <Checkbox checked={subscriptionPlanForm.is_active} onChange={(event) => setSubscriptionPlanForm((current) => ({ ...current, is_active: event.target.checked }))}>启用套餐</Checkbox>
          </div>
          <FormActions submitLabel="保存套餐" onCancel={closeModal} onSubmit={createSubscriptionPlan} disabled={!subscriptionPlanForm.plan_type.trim() || !subscriptionPlanForm.name.trim()} />
        </Modal>
      )}

      {modal?.type === 'adminUserEdit' && (
        <Modal title="用户管理" size="large" onClose={closeModal}>
          <div className="policy-grid">
            <label>用户名称<Input value={adminUserForm.name} onChange={(event) => setAdminUserForm((current) => ({ ...current, name: event.target.value }))} /></label>
            <label>邮箱<Input value={adminUserForm.email} onChange={(event) => setAdminUserForm((current) => ({ ...current, email: event.target.value }))} /></label>
            <label>手机号<Input value={adminUserForm.phone} onChange={(event) => setAdminUserForm((current) => ({ ...current, phone: event.target.value }))} /></label>
            <label>角色<Select value={adminUserForm.role} onChange={(value) => setAdminUserForm((current) => ({ ...current, role: value }))} options={['普通用户', '系统管理员'].map((value) => ({ value, label: value }))} /></label>
            <label>订阅状态<Select value={adminUserForm.subscription_status} onChange={(value) => setAdminUserForm((current) => ({ ...current, subscription_status: value }))} options={[{ value: 'pending_activation', label: '待激活' }, { value: 'active', label: '已激活' }, { value: 'expired', label: '已过期' }]} /></label>
            <Checkbox checked={adminUserForm.is_active} onChange={(event) => setAdminUserForm((current) => ({ ...current, is_active: event.target.checked }))}>允许登录</Checkbox>
            <div className="wide-field">
              <span className="field-label">可见菜单</span>
              <div className="choice-grid">
                {[
                  ['overview', '运营概览'],
                  ['accounts', '账号管理'],
                  ['taskManagement', '任务管理'],
                  ['groupManagement', '群聊管理'],
                  ['usageReports', '用户用量'],
                  ['audits', '审计安全'],
                ].map(([value, label]) => (
                  <Checkbox
                    key={value}
                    checked={adminUserForm.menu_permissions.includes(value)}
                    onChange={(event) => {
                      const next = event.target.checked
                        ? [...adminUserForm.menu_permissions, value]
                        : adminUserForm.menu_permissions.filter((item) => item !== value);
                      setAdminUserForm((current) => ({ ...current, menu_permissions: Array.from(new Set(next)) }));
                    }}
                  >
                    {label}
                  </Checkbox>
                ))}
              </div>
            </div>
            <label>Token 调整<InputNumber value={tokenAdjustmentForm.delta_tokens} onChange={(value) => setTokenAdjustmentForm((current) => ({ ...current, delta_tokens: Number(value ?? 0) }))} /></label>
            <label>调整原因<Input value={tokenAdjustmentForm.reason} onChange={(event) => setTokenAdjustmentForm((current) => ({ ...current, reason: event.target.value }))} /></label>
          </div>
          <div className="mini-list">
            {selectedUserTokenLedgers.slice(0, 5).map((ledger) => (
              <p key={ledger.id} className="muted-line">{ledger.created_at.replace('T', ' ').slice(0, 16)} / {ledger.change_type} / {ledger.delta_tokens} / 余额 {ledger.balance_after}</p>
            ))}
          </div>
          <Space className="modal-actions">
            <Button onClick={closeModal}>取消</Button>
            <Button onClick={() => {
              const user = adminUsers.find((item) => item.id === adminUserForm.id);
              if (user) void resetAdminUserPassword(user, 'user123456');
            }}>重置密码</Button>
            <Button onClick={() => {
              const user = adminUsers.find((item) => item.id === adminUserForm.id);
              if (user) void adjustAdminUserTokens(user);
            }}>调整 Token</Button>
            <Button type="primary" onClick={saveAdminUser} disabled={!adminUserForm.name.trim() || !adminUserForm.email.trim()}>保存用户</Button>
          </Space>
        </Modal>
      )}

      {modal?.type === 'tenantAiEdit' && tenantAiSetting && (
        <Modal title="编辑客户 AI 配置" size="medium" onClose={closeModal}>
          <div className="policy-grid">
            <label>默认模型<Select<number | ''> value={selectedAiProviderId || ''} disabled={!aiProviders.length} onChange={(value) => setSelectedAiProviderId(Number(value) || '')} options={aiProviders.length ? aiProviders.map((provider) => ({ value: provider.id, label: `${provider.provider_name} / ${provider.model_name}` })) : [{ value: '', label: '请先新增 AI 供应商' }]} /></label>
            <label>温度<InputNumber min={0} max={2} step={0.1} value={tenantAiSetting.temperature} onChange={(value) => setTenantAiSetting({ ...tenantAiSetting, temperature: Number(value ?? 0) })} /></label>
            <label>最大 Token<InputNumber min={128} max={8192} value={tenantAiSetting.max_tokens} onChange={(value) => setTenantAiSetting({ ...tenantAiSetting, max_tokens: Number(value ?? 128) })} /></label>
            <Checkbox checked={tenantAiSetting.ai_enabled} onChange={(event) => setTenantAiSetting({ ...tenantAiSetting, ai_enabled: event.target.checked })}>启用 AI 草稿</Checkbox>
            <Checkbox checked={tenantAiSetting.fallback_to_mock} onChange={(event) => setTenantAiSetting({ ...tenantAiSetting, fallback_to_mock: event.target.checked })}>失败回退模板</Checkbox>
          </div>
          <FormActions onCancel={closeModal} onSubmit={saveTenantAiSetting} disabled={!aiProviders.length} />
        </Modal>
      )}

      {modal?.type === 'schedulingEdit' && (
        <Modal title="编辑发送节奏" size="medium" onClose={closeModal}>
          <div className="policy-grid">
            <label>最小抖动秒<InputNumber min={0} value={jitterMinSeconds} onChange={(value) => setJitterMinSeconds(Number(value ?? 0))} /></label>
            <label>最大抖动秒<InputNumber min={0} value={jitterMaxSeconds} onChange={(value) => setJitterMaxSeconds(Number(value ?? 0))} /></label>
            <label>批次间隔秒<InputNumber min={0} value={batchIntervalSeconds} onChange={(value) => setBatchIntervalSeconds(Number(value ?? 0))} /></label>
            <Checkbox checked={respectSendWindow} onChange={(event) => setRespectSendWindow(event.target.checked)}>遵守发送时间窗</Checkbox>
          </div>
          <FormActions onCancel={closeModal} onSubmit={saveSchedulingSetting} />
        </Modal>
      )}

      {modal?.type === 'changePassword' && (
        <Modal title="修改登录密码" size="small" onClose={closeModal}>
          <div className="policy-grid">
            <label className="wide-field">当前密码<Input.Password value={changePasswordForm.current_password} onChange={(event) => setChangePasswordForm((current) => ({ ...current, current_password: event.target.value }))} /></label>
            <label className="wide-field">新密码<Input.Password value={changePasswordForm.new_password} onChange={(event) => setChangePasswordForm((current) => ({ ...current, new_password: event.target.value }))} /></label>
            <label className="wide-field">确认新密码<Input.Password value={changePasswordForm.confirm_password} onChange={(event) => setChangePasswordForm((current) => ({ ...current, confirm_password: event.target.value }))} /></label>
          </div>
          <FormActions submitLabel="修改密码" onCancel={closeModal} onSubmit={changePassword} disabled={!changePasswordForm.current_password || changePasswordForm.new_password.length < 6 || changePasswordForm.new_password !== changePasswordForm.confirm_password} />
        </Modal>
      )}

      {modal?.type === 'promptTemplateCreate' && (
        <Modal title="新增提示词模板" size="large" onClose={closeModal}>
          <div className="policy-grid">
            <label>模板名称<Input value={promptTemplateForm.name} onChange={(event) => setPromptTemplateForm({ ...promptTemplateForm, name: event.target.value })} /></label>
            <label>模板类型<Select value={promptTemplateForm.template_type} onChange={(value) => setPromptTemplateForm({ ...promptTemplateForm, template_type: value })} options={['系统决策提示词', '群活跃草稿', '多账号对话脚本', '素材配文', '风险检查'].map((value) => ({ value, label: value }))} /></label>
            <label className="wide-field">模板内容<Input.TextArea value={promptTemplateForm.content} onChange={(event) => setPromptTemplateForm({ ...promptTemplateForm, content: event.target.value })} /></label>
          </div>
          <FormActions submitLabel="新增提示词" onCancel={closeModal} onSubmit={createPromptTemplate} disabled={!promptTemplateForm.name || !promptTemplateForm.content} />
        </Modal>
      )}

      {modal?.type === 'materialCreate' && (
        <Modal title="新增素材" size="medium" onClose={closeModal}>
          <div className="policy-grid">
            <label>素材标题<Input value={materialForm.title} onChange={(event) => setMaterialForm({ ...materialForm, title: event.target.value })} /></label>
            <label>素材类型<Select value={materialForm.material_type} onChange={(value) => setMaterialForm({ ...materialForm, material_type: value })} options={['文本', '图片', '表情包', '文件', '链接', '组合消息'].map((value) => ({ value, label: value }))} /></label>
            <label>标签<Input value={materialForm.tags} onChange={(event) => setMaterialForm({ ...materialForm, tags: event.target.value })} /></label>
            <label className="wide-field">内容/URL<Input.TextArea value={materialForm.content} onChange={(event) => setMaterialForm({ ...materialForm, content: event.target.value })} /></label>
          </div>
          <FormActions submitLabel="新增素材" onCancel={closeModal} onSubmit={createMaterial} disabled={!materialForm.title || !materialForm.content} />
        </Modal>
      )}

      {(modal?.type === 'keywordRuleCreate' || modal?.type === 'keywordRuleEdit') && (
        <Modal title={modal.type === 'keywordRuleEdit' ? '编辑关键词' : '新增关键词'} size="medium" onClose={closeModal}>
          <div className="policy-grid">
            <label>关键词<Input value={keywordRuleForm.keyword} onChange={(event) => setKeywordRuleForm({ ...keywordRuleForm, keyword: event.target.value })} /></label>
            <label>匹配方式<Select value={keywordRuleForm.match_type} onChange={(value) => setKeywordRuleForm({ ...keywordRuleForm, match_type: value })} options={[{ value: 'contains', label: '包含' }]} /></label>
            <label className="wide-field">备注<Input value={keywordRuleForm.note} onChange={(event) => setKeywordRuleForm({ ...keywordRuleForm, note: event.target.value })} /></label>
            <Checkbox checked={keywordRuleForm.is_active} onChange={(event) => setKeywordRuleForm({ ...keywordRuleForm, is_active: event.target.checked })}>启用关键词</Checkbox>
          </div>
          <FormActions submitLabel={modal.type === 'keywordRuleEdit' ? '保存关键词' : '新增关键词'} onCancel={closeModal} onSubmit={modal.type === 'keywordRuleEdit' ? saveContentKeywordRule : createContentKeywordRule} disabled={!keywordRuleForm.keyword.trim()} />
        </Modal>
      )}

      {modal?.type === 'accountPoolDetail' && accountPoolDetail && (
        <AccountPoolDetailModal accountPoolDetail={accountPoolDetail} poolDirectAccountId={poolDirectAccountId} setPoolDirectAccountId={setPoolDirectAccountId} directMessageForm={directMessageForm} setDirectMessageForm={setDirectMessageForm} selectedDirectContact={selectedDirectContact} onClose={closeModal} onOpenAccountCreate={openAccountCreate} onOpenAccountDetail={openAccountDetail} onRefreshAccountPoolDetail={refreshAccountPoolDetail} onStartDirectMessageToContact={startDirectMessageToContact} onCreateDirectMessageTask={createDirectMessageTask} onOpenConfirm={openConfirm} onSetReturnAfterVerification={setReturnAfterVerification} onSetModal={ctx.setModal} accountName={accountName} />
      )}

      {modal?.type === 'accountPoolCreate' && (
        <Modal title="新增账号分组" size="medium" onClose={closeModal}>
          <div className="policy-grid">
            <label>账号分组名称<Input value={accountPoolForm.name} onChange={(event) => setAccountPoolForm({ ...accountPoolForm, name: event.target.value })} /></label>
            <Checkbox checked={accountPoolForm.is_default} onChange={(event) => setAccountPoolForm({ ...accountPoolForm, is_default: event.target.checked })}>设为默认账号分组</Checkbox>
            <label className="wide-field">说明<Input.TextArea value={accountPoolForm.description} onChange={(event) => setAccountPoolForm({ ...accountPoolForm, description: event.target.value })} /></label>
          </div>
          <FormActions submitLabel="新增账号分组" onCancel={closeModal} onSubmit={createAccountPool} disabled={!accountPoolForm.name.trim()} />
        </Modal>
      )}

      {modal?.type === 'accountCreate' && (
        <Modal title="新增账号" size="medium" onClose={closeModal}>
          <div className="policy-grid">
            <label>所属账号分组<Select value={accountCreateForm.pool_id || ''} onChange={(value) => setAccountCreateForm({ ...accountCreateForm, pool_id: Number(value) || '' })} options={[{ value: '', label: '默认账号分组' }, ...accountPools.map((pool) => ({ value: pool.id, label: pool.name }))]} /></label>
            <label>登录方式<Select value={accountCreateForm.login_method} onChange={(value) => setAccountCreateForm({ ...accountCreateForm, login_method: value as 'code' | 'qr' })} options={[{ value: 'code', label: '手机号验证码' }, { value: 'qr', label: '二维码扫码' }]} /></label>
            <label>平台备注名<Input value={accountCreateForm.display_name} onChange={(event) => setAccountCreateForm({ ...accountCreateForm, display_name: event.target.value })} /></label>
            <label>TG 用户名<Input value={accountCreateForm.username} onChange={(event) => setAccountCreateForm({ ...accountCreateForm, username: event.target.value })} placeholder="可选，不含 @" /></label>
            <label className="wide-field">手机号<Input value={accountCreateForm.phone_number} onChange={(event) => setAccountCreateForm({ ...accountCreateForm, phone_number: event.target.value })} placeholder="+8613800000000" /></label>
          </div>
          <p className="muted-line">创建后会进入所选登录方式；验证码和扫码是同级二选一流程。</p>
          <FormActions submitLabel="创建账号" onCancel={closeModal} onSubmit={createAccount} disabled={!accountCreateForm.display_name.trim() || !accountCreateForm.phone_number.trim()} />
        </Modal>
      )}

      {modal?.type === 'accountLogin' && accountLoginForm.account && (
        <Modal title={`${accountLoginForm.account.display_name} 完成登录`} size="small" onClose={closeModal}>
          <div className="detail-list">
            <div><dt>账号</dt><dd>{accountLoginForm.account.phone_masked}</dd></div>
            <div><dt>当前状态</dt><dd><StatusBadge status={accountLoginForm.account.status} /></dd></div>
            <div><dt>登录方式</dt><dd>{accountLoginForm.method === 'qr' ? '二维码扫码' : '手机号验证码'}</dd></div>
            {accountLoginForm.flow?.code_expires_at && <div><dt>验证码有效期</dt><dd>{new Date(accountLoginForm.flow.code_expires_at).toLocaleTimeString()}</dd></div>}
          </div>
          {accountLoginForm.step === 'qr' && (
            <>
              <div className="policy-grid">
                <label className="wide-field">扫码 payload
                  <Input.TextArea value={accountLoginForm.flow?.qr_payload ?? ''} readOnly autoSize={{ minRows: 4, maxRows: 8 }} placeholder="二维码 payload 将在启动扫码登录后展示" />
                </label>
              </div>
              {accountLoginForm.error && <p className="danger-text">{accountLoginForm.error}</p>}
              <Space className="modal-actions">
                <Button onClick={resendAccountLoginCode} disabled={Boolean(busy)}>改用验证码</Button>
                <Button type="primary" onClick={checkAccountQrLogin} disabled={Boolean(busy)}>检查扫码结果</Button>
              </Space>
            </>
          )}
          {accountLoginForm.step === 'code' && (
            <>
              <div className="policy-grid">
                <label className="wide-field">验证码
                  <Input
                    value={accountLoginForm.code}
                    onChange={(event) => setAccountLoginForm((current) => ({ ...current, code: event.target.value, error: '' }))}
                    placeholder="输入 Telegram 收到的验证码"
                    autoFocus
                  />
                </label>
              </div>
              {accountLoginForm.flow?.code_preview && <p className="muted-line">开发模式验证码：{accountLoginForm.flow.code_preview}</p>}
              {accountLoginForm.error && <p className="danger-text">{accountLoginForm.error}</p>}
              <Space className="modal-actions">
                <Button onClick={resendAccountLoginCode} disabled={Boolean(busy)}>{accountLoginForm.flow ? '重新发送验证码' : '获取验证码'}</Button>
                <Button type="primary" onClick={submitAccountLoginCode} disabled={Boolean(busy) || !accountLoginForm.code.trim()}>提交验证码</Button>
              </Space>
            </>
          )}
          {accountLoginForm.step === 'password' && (
            <>
              <div className="policy-grid">
                <label className="wide-field">二步验证密码
                  <Input.Password
                    value={accountLoginForm.password_2fa}
                    onChange={(event) => setAccountLoginForm((current) => ({ ...current, password_2fa: event.target.value, error: '' }))}
                    placeholder="输入 Telegram 2FA 密码"
                    autoFocus
                  />
                </label>
              </div>
              {accountLoginForm.error && <p className="danger-text">{accountLoginForm.error}</p>}
              <Space className="modal-actions">
                <Button onClick={() => setAccountLoginForm((current) => ({ ...current, step: 'code', password_2fa: '', error: '' }))}>返回验证码</Button>
                <Button type="primary" onClick={submitAccountLoginPassword} disabled={Boolean(busy) || !accountLoginForm.password_2fa}>完成登录</Button>
              </Space>
            </>
          )}
        </Modal>
      )}

      {modal?.type === 'accountMovePool' && accountDetail && (
        <Modal title="移动账号分组" size="small" onClose={() => ctx.setModal({ type: 'accountDetail' })}>
          <div className="policy-grid">
            <label>目标账号分组<Select value={accountDetail.account.pool_id ?? ''} onChange={(value) => moveCurrentAccountPool(Number(value))} options={accountPools.map((pool) => ({ value: pool.id, label: pool.name }))} /></label>
          </div>
          <Space className="modal-actions"><Button onClick={() => ctx.setModal({ type: 'accountDetail' })}>返回</Button></Space>
        </Modal>
      )}

      {modal?.type === 'accountCloneCreate' && accountDetail && (
        <Modal title="创建账号克隆计划" size="medium" onClose={() => ctx.setModal({ type: 'accountDetail' })}>
          <div className="target-account-grid">
            {accounts.filter((account) => account.id !== accountDetail.account.id).map((account) => {
              const selected = cloneForm.target_account_ids.includes(account.id);
              return (
                <Button key={account.id} className={selected ? 'selected contact-pick' : 'contact-pick'} onClick={() => setCloneForm({ ...cloneForm, target_account_ids: selected ? cloneForm.target_account_ids.filter((id) => id !== account.id) : [...cloneForm.target_account_ids, account.id] })}>
                  <strong>{account.display_name}</strong><span>{account.pool_name}</span><StatusBadge status={account.status} />
                </Button>
              );
            })}
          </div>
          <div className="policy-grid">
            <Checkbox checked={cloneForm.clone_contacts} onChange={(event) => setCloneForm({ ...cloneForm, clone_contacts: event.target.checked })}>克隆好友和私聊对象</Checkbox>
            <Checkbox checked={cloneForm.clone_groups} onChange={(event) => setCloneForm({ ...cloneForm, clone_groups: event.target.checked })}>克隆群聊和频道清单</Checkbox>
          </div>
          <p className="muted-line">已选择 {cloneForm.target_account_ids.length} 个目标账号。系统会先生成计划，确认后逐项执行。</p>
          <FormActions submitLabel="生成克隆计划" onCancel={() => ctx.setModal({ type: 'accountDetail' })} onSubmit={createClonePlan} disabled={!cloneForm.target_account_ids.length || (!cloneForm.clone_contacts && !cloneForm.clone_groups)} />
        </Modal>
      )}

      {modal?.type === 'verificationTaskDetail' && (
        <Modal title="验证辅助处理" size="medium" onClose={() => ctx.setModal({ type: returnAfterVerification })}>
          <div className="detail-list">
            <div><dt>状态</dt><dd><StatusBadge status={modal.payload.status} /></dd></div>
            <div><dt>验证类型</dt><dd>{modal.payload.verification_type}</dd></div>
            <div><dt>建议操作</dt><dd>{modal.payload.suggested_action}</dd></div>
            <div><dt>目标</dt><dd>{modal.payload.target_display || modal.payload.target_peer_id || '当前群聊'}</dd></div>
          </div>
          <p className="dialog-message">{modal.payload.detected_reason || '平台检测到当前账号在该群可能需要完成验证后才能发言。'}</p>
          <p className="muted-line">平台只会在你确认后执行可控动作。</p>
          <Space className="modal-actions">
            <Button onClick={() => ctx.setModal({ type: returnAfterVerification })}>返回</Button>
            <Button onClick={() => dismissVerificationTask(modal.payload)}>忽略</Button>
            <Button type="primary" disabled={!['待处理', '失败'].includes(modal.payload.status)} onClick={() => confirmVerificationTask(modal.payload)}>确认处理</Button>
          </Space>
        </Modal>
      )}

      {modal?.type === 'groupPolicyEdit' && selectedGroup && (
        <Modal title="编辑群运营配置" size="large" onClose={closeModal}>
          <div className="policy-grid">
            <label>活跃时间<Input value={groupPolicy.active_window} onChange={(event) => setGroupPolicy({ ...groupPolicy, active_window: event.target.value })} /></label>
            <label>每日上限<InputNumber value={groupPolicy.daily_limit} onChange={(value) => setGroupPolicy({ ...groupPolicy, daily_limit: Number(value ?? 0) })} /></label>
            <label>账号冷却秒<InputNumber value={groupPolicy.account_cooldown_seconds} onChange={(value) => setGroupPolicy({ ...groupPolicy, account_cooldown_seconds: Number(value ?? 0) })} /></label>
            <label>群冷却秒<InputNumber value={groupPolicy.group_cooldown_seconds} onChange={(value) => setGroupPolicy({ ...groupPolicy, group_cooldown_seconds: Number(value ?? 0) })} /></label>
            <label>话题方向<Input.TextArea value={groupPolicy.topic_direction} onChange={(event) => setGroupPolicy({ ...groupPolicy, topic_direction: event.target.value })} /></label>
            <label>禁用词<Input.TextArea value={groupPolicy.banned_words} onChange={(event) => setGroupPolicy({ ...groupPolicy, banned_words: event.target.value })} /></label>
            <label>链接白名单<Input.TextArea value={groupPolicy.link_whitelist} onChange={(event) => setGroupPolicy({ ...groupPolicy, link_whitelist: event.target.value })} /></label>
            <Checkbox checked={groupPolicy.require_review} onChange={(event) => setGroupPolicy({ ...groupPolicy, require_review: event.target.checked })}>需要人工审核</Checkbox>
            <Checkbox checked={groupPolicy.listener_enabled} onChange={(event) => setGroupPolicy({ ...groupPolicy, listener_enabled: event.target.checked })}>启用监听续聊</Checkbox>
            <Checkbox checked={groupPolicy.listener_auto_reply_enabled} onChange={(event) => setGroupPolicy({ ...groupPolicy, listener_auto_reply_enabled: event.target.checked })}>监听触发后自动发送</Checkbox>
            <label>监听间隔秒<InputNumber min={30} value={groupPolicy.listener_interval_seconds} onChange={(value) => setGroupPolicy({ ...groupPolicy, listener_interval_seconds: Number(value ?? 30) })} /></label>
            <label>上下文条数<InputNumber min={1} max={100} value={groupPolicy.listener_context_limit} onChange={(value) => setGroupPolicy({ ...groupPolicy, listener_context_limit: Number(value ?? 1) })} /></label>
            <div className="wide-field">
              <span className="field-label">监听号</span>
              <div className="choice-grid">
                {(groupDetail?.group.id === selectedGroup.id ? groupDetail.accounts : accounts).map((account) => (
                  <Checkbox
                    key={account.id}
                      checked={groupPolicy.listener_account_ids.includes(account.id)}
                      onChange={(event) => {
                        const nextIds = event.target.checked
                          ? [...groupPolicy.listener_account_ids, account.id]
                          : groupPolicy.listener_account_ids.filter((id) => id !== account.id);
                        setGroupPolicy({ ...groupPolicy, listener_account_ids: Array.from(new Set(nextIds)) });
                      }}
                    >
                    {account.display_name}{account.username ? ` / @${account.username}` : ''} / {account.status}
                  </Checkbox>
                ))}
              </div>
            </div>
          </div>
          <FormActions onCancel={closeModal} onSubmit={saveGroupPolicy} />
        </Modal>
      )}

      {modal?.type === 'accountProfileEdit' && accountDetail && (
        <Modal title="编辑账号资料" size="medium" onClose={() => ctx.setModal({ type: 'accountDetail' })}>
          <div className="profile-edit-layout">
            <div className="avatar-preview">
              {avatarFile ? <img src={URL.createObjectURL(avatarFile)} alt="" /> : profileForm.avatar_object_key ? <img src={avatarUrl(`/media/${profileForm.avatar_object_key}`)} alt="" /> : <span>{profileForm.display_name.slice(0, 1) || 'T'}</span>}
            </div>
            <div className="policy-grid">
              <label>平台备注名<Input value={profileForm.display_name} onChange={(event) => setProfileForm({ ...profileForm, display_name: event.target.value })} /></label>
              <label>TG 名<Input value={profileForm.tg_first_name} onChange={(event) => setProfileForm({ ...profileForm, tg_first_name: event.target.value })} /></label>
              <label>TG 姓<Input value={profileForm.tg_last_name} onChange={(event) => setProfileForm({ ...profileForm, tg_last_name: event.target.value })} /></label>
              <label className="wide-field">头像上传<input type="file" accept={runtime?.avatar_allowed_types.join(',') ?? 'image/jpeg,image/png,image/webp'} onChange={(event) => setAvatarFile(event.target.files?.[0] ?? null)} /></label>
              <label className="wide-field">TG 简介<Input.TextArea value={profileForm.tg_bio} maxLength={220} onChange={(event) => setProfileForm({ ...profileForm, tg_bio: event.target.value })} /></label>
            </div>
          </div>
          <p className="muted-line">头像最大 {Math.round((runtime?.avatar_max_bytes ?? 0) / 1024 / 1024) || 2}MB；保存后会自动进入后台同步处理。</p>
          <FormActions submitLabel="保存并同步" onCancel={() => ctx.setModal({ type: 'accountDetail' })} onSubmit={saveAccountProfile} disabled={!profileForm.display_name.trim()} />
        </Modal>
      )}

      {modal?.type === 'draftEdit' && draftEditTarget && (
        <Modal title="编辑草稿" size="medium" onClose={closeModal}>
          <div className="policy-grid">
            <label>风险等级<Select value={draftEditForm.risk_level} onChange={(value) => setDraftEditForm({ ...draftEditForm, risk_level: value })} options={['低', '中', '高'].map((value) => ({ value, label: value }))} /></label>
            <label>建议账号<Select value={draftEditForm.suggested_account_id || ''} onChange={(value) => setDraftEditForm({ ...draftEditForm, suggested_account_id: Number(value) || '' })} options={[{ value: '', label: '不指定' }, ...accounts.map((account) => ({ value: account.id, label: account.display_name }))]} /></label>
            <label className="wide-field">草稿内容<Input.TextArea value={draftEditForm.content} onChange={(event) => setDraftEditForm({ ...draftEditForm, content: event.target.value })} /></label>
          </div>
          <FormActions onCancel={closeModal} onSubmit={saveDraftEdit} disabled={!draftEditForm.content.trim()} />
        </Modal>
      )}

      {modal?.type === 'accountDetail' && accountDetail && (
        <AccountDetailModal accountDetail={accountDetail} accountDetailTab={accountDetailTab} setAccountDetailTab={setAccountDetailTab} runtime={runtime} directMessageForm={directMessageForm} setDirectMessageForm={setDirectMessageForm} selectedDirectContact={selectedDirectContact} accountContacts={accountContacts} accounts={accounts} avatarUrl={avatarUrl} onClose={closeModal} onOpenAccountProfileEdit={openAccountProfileEdit} onQueueAccountSyncNow={queueAccountSyncNow} onPollVerificationCodes={pollVerificationCodes} onStartDirectMessageToContact={startDirectMessageToContact} onCreateDirectMessageTask={createDirectMessageTask} onConfirmClonePlan={confirmClonePlan} onRetryCloneItem={ctx.retryCloneItem} onRetryAccountProfileSync={retryAccountProfileSync} onDismissVerificationTask={dismissVerificationTask} onConfirmVerificationTask={confirmVerificationTask} onOpenConfirm={openConfirm} onSetReturnAfterVerification={setReturnAfterVerification} onSetModal={ctx.setModal} onSetCloneForm={setCloneForm} accountName={accountName} />
      )}

      {modal?.type === 'campaignCreate' && (
        <CampaignWizard groups={groups} aiProviders={aiProviders} materials={materials} campaignStep={campaignStep} setCampaignStep={setCampaignStep} campaignMode={campaignMode} setCampaignMode={setCampaignMode} selectedTargetGroupIds={selectedTargetGroupIds} selectedSourceGroupIds={selectedSourceGroupIds} recommendedAccounts={recommendedAccounts} selectedAccountsByGroup={selectedAccountsByGroup} targetGroupsMissingAccounts={targetGroupsMissingAccounts} topic={topic} setTopic={setTopic} sendWindow={sendWindow} setSendWindow={setSendWindow} intensity={intensity} setIntensity={setIntensity} draftCount={draftCount} setDraftCount={setDraftCount} tone={tone} setTone={setTone} selectedAiProviderId={selectedAiProviderId} setSelectedAiProviderId={setSelectedAiProviderId} selectedMaterialIds={selectedMaterialIds} jitterMinSeconds={jitterMinSeconds} setJitterMinSeconds={setJitterMinSeconds} jitterMaxSeconds={jitterMaxSeconds} setJitterMaxSeconds={setJitterMaxSeconds} batchIntervalSeconds={batchIntervalSeconds} setBatchIntervalSeconds={setBatchIntervalSeconds} respectSendWindow={respectSendWindow} setRespectSendWindow={setRespectSendWindow} campaignEndsAt={campaignEndsAt} setCampaignEndsAt={setCampaignEndsAt} maxAiTokens={maxAiTokens} setMaxAiTokens={setMaxAiTokens} runIntervalSeconds={runIntervalSeconds} setRunIntervalSeconds={setRunIntervalSeconds} participationMinRatio={participationMinRatio} setParticipationMinRatio={setParticipationMinRatio} participationMaxRatio={participationMaxRatio} setParticipationMaxRatio={setParticipationMaxRatio} maxMessagesPerAccount={maxMessagesPerAccount} setMaxMessagesPerAccount={setMaxMessagesPerAccount} maxDraftsPerBatch={maxDraftsPerBatch} setMaxDraftsPerBatch={setMaxDraftsPerBatch} onClose={closeModal} onToggleTargetGroup={toggleTargetGroup} onToggleSourceGroup={toggleSourceGroup} onGoAccountStep={goCampaignAccountStep} onGoContentStep={goCampaignContentStep} onToggleRecommendedAccount={toggleRecommendedAccount} onSetGroupAccountsSelected={setGroupAccountsSelected} onToggleMaterial={toggleMaterial} onCreateCampaignAndDrafts={createCampaignAndDrafts} groupName={groupName} />
      )}

      {modal?.type === 'confirmAction' && <ConfirmDialog payload={modal.payload} onClose={closeModal} />}
      {resultDialog && <ResultDialog dialog={resultDialog} onClose={() => setResultDialog(null)} />}
    </>
  );
}
