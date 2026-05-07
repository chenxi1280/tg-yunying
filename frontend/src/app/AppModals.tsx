import { useAppContext } from './context';
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
    // Tenant AI
    tenantAiSetting, setTenantAiSetting, selectedAiProviderId, setSelectedAiProviderId, aiProviders, saveTenantAiSetting,
    // Scheduling
    jitterMinSeconds, setJitterMinSeconds, jitterMaxSeconds, setJitterMaxSeconds, batchIntervalSeconds, setBatchIntervalSeconds, respectSendWindow, setRespectSendWindow, saveSchedulingSetting,
    // Prompt Template
    promptTemplateForm, setPromptTemplateForm, createPromptTemplate,
    // Material
    materialForm, setMaterialForm, createMaterial,
    // Account Pool
    accountPoolForm, setAccountPoolForm, createAccountPool, accountPoolDetail, poolDirectAccountId, setPoolDirectAccountId, refreshAccountPoolDetail,
    // Account
    accountCreateForm, setAccountCreateForm, createAccount, loginAfterCreate, accountLoginForm, setAccountLoginForm, submitAccountLoginCode, submitAccountLoginPassword, resendAccountLoginCode, accountPools, accounts, accountDetail, setAccountDetailTab, accountDetailTab, runtime, cloneForm, setCloneForm, createClonePlan, confirmClonePlan, moveCurrentAccountPool,
    // Profile
    profileForm, setProfileForm, avatarFile, setAvatarFile, avatarUrl, saveAccountProfile, openAccountProfileEdit, queueAccountSyncNow, pollVerificationCodes, retryAccountProfileSync,
    // Group
    selectedGroup, groupPolicy, setGroupPolicy, saveGroupPolicy, groupDetail,
    // Campaign
    groups, campaigns, materials, campaignStep, setCampaignStep, selectedTargetGroupIds, recommendedAccounts, selectedAccountsByGroup, targetGroupsMissingAccounts, topic, setTopic, sendWindow, setSendWindow, intensity, setIntensity, draftCount, setDraftCount, tone, setTone, selectedMaterialIds, toggleTargetGroup, goCampaignAccountStep, goCampaignContentStep, toggleRecommendedAccount, setGroupAccountsSelected, toggleMaterial, createCampaignAndDrafts, selectedCampaignId, setSelectedCampaignId,
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
            <label>应用名称<input value={developerAppForm.app_name} onChange={(event) => setDeveloperAppForm({ ...developerAppForm, app_name: event.target.value })} /></label>
            <label>API ID<input value={developerAppForm.api_id} onChange={(event) => setDeveloperAppForm({ ...developerAppForm, api_id: event.target.value })} /></label>
            <label>账号上限<input type="number" min={0} value={developerAppForm.max_accounts} onChange={(event) => setDeveloperAppForm({ ...developerAppForm, max_accounts: Number(event.target.value) })} /></label>
            <label>备注<input value={developerAppForm.notes} onChange={(event) => setDeveloperAppForm({ ...developerAppForm, notes: event.target.value })} /></label>
            <label className="wide-field">API Hash<input type="password" value={developerAppForm.api_hash} onChange={(event) => setDeveloperAppForm({ ...developerAppForm, api_hash: event.target.value })} placeholder={modal.type === 'developerAppEdit' ? '不填写则保留原凭证' : ''} /></label>
            <label className="checkbox-line"><input type="checkbox" checked={developerAppForm.is_active} onChange={(event) => setDeveloperAppForm({ ...developerAppForm, is_active: event.target.checked })} />启用应用</label>
          </div>
          <FormActions submitLabel={modal.type === 'developerAppEdit' ? '保存应用' : '新增应用'} onCancel={closeModal} onSubmit={createDeveloperApp} disabled={!developerAppForm.app_name.trim() || !developerAppForm.api_id || (modal.type === 'developerAppCreate' && developerAppForm.api_hash.length < 8)} />
        </Modal>
      )}

      {(modal?.type === 'aiProviderCreate' || modal?.type === 'aiProviderEdit') && (
        <Modal title={modal.type === 'aiProviderEdit' ? '编辑 AI 供应商' : '新增 AI 供应商'} size="medium" onClose={closeModal}>
          <div className="policy-grid">
            <label>名称<input value={aiProviderForm.provider_name} onChange={(event) => setAiProviderForm({ ...aiProviderForm, provider_name: event.target.value })} /></label>
            <label>Base URL<input value={aiProviderForm.base_url} onChange={(event) => setAiProviderForm({ ...aiProviderForm, base_url: event.target.value })} /></label>
            <label>模型名<input value={aiProviderForm.model_name} onChange={(event) => setAiProviderForm({ ...aiProviderForm, model_name: event.target.value })} /></label>
            <label>Key Header<input value={aiProviderForm.api_key_header} onChange={(event) => setAiProviderForm({ ...aiProviderForm, api_key_header: event.target.value })} /></label>
            <label className="wide-field">API Key<input type="password" value={aiProviderForm.api_key} onChange={(event) => setAiProviderForm({ ...aiProviderForm, api_key: event.target.value })} placeholder={modal.type === 'aiProviderEdit' ? '不填写则保留原 Key' : ''} /></label>
            <label className="wide-field">备注<input value={aiProviderForm.notes} onChange={(event) => setAiProviderForm({ ...aiProviderForm, notes: event.target.value })} /></label>
            <label className="checkbox-line"><input type="checkbox" checked={aiProviderForm.is_active} onChange={(event) => setAiProviderForm({ ...aiProviderForm, is_active: event.target.checked })} />启用供应商</label>
          </div>
          <FormActions submitLabel={modal.type === 'aiProviderEdit' ? '保存供应商' : '新增供应商'} onCancel={closeModal} onSubmit={createAiProvider} disabled={!aiProviderForm.provider_name.trim() || !aiProviderForm.base_url.trim() || !aiProviderForm.model_name.trim() || (modal.type === 'aiProviderCreate' && aiProviderForm.api_key.length < 4)} />
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
              <select value={selectedAiProviderId} disabled={!aiProviders.length} onChange={(event) => setSelectedAiProviderId(Number(event.target.value))}>
                {!aiProviders.length && <option value="">请先新增 AI 供应商</option>}
                {aiProviders.map((provider) => <option key={provider.id} value={provider.id}>{provider.provider_name} / {provider.model_name}</option>)}
              </select>
            </label>
            <label>温度<input type="number" min={0} max={2} step={0.1} value={tenantAiSetting.temperature} onChange={(event) => setTenantAiSetting({ ...tenantAiSetting, temperature: Number(event.target.value) })} /></label>
            <label>最大 Token<input type="number" min={128} max={8192} value={tenantAiSetting.max_tokens} onChange={(event) => setTenantAiSetting({ ...tenantAiSetting, max_tokens: Number(event.target.value) })} /></label>
            <label className="checkbox-line"><input type="checkbox" checked={tenantAiSetting.ai_enabled} onChange={(event) => setTenantAiSetting({ ...tenantAiSetting, ai_enabled: event.target.checked })} />启用 AI 草稿</label>
            <label className="checkbox-line"><input type="checkbox" checked={tenantAiSetting.fallback_to_mock} onChange={(event) => setTenantAiSetting({ ...tenantAiSetting, fallback_to_mock: event.target.checked })} />失败回退模板</label>
          </div>
          <FormActions onCancel={closeModal} onSubmit={saveTenantAiSetting} disabled={!aiProviders.length} />
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

      {modal?.type === 'changePassword' && (
        <Modal title="修改登录密码" size="small" onClose={closeModal}>
          <div className="policy-grid">
            <label className="wide-field">当前密码<input type="password" value={changePasswordForm.current_password} onChange={(event) => setChangePasswordForm((current) => ({ ...current, current_password: event.target.value }))} /></label>
            <label className="wide-field">新密码<input type="password" value={changePasswordForm.new_password} onChange={(event) => setChangePasswordForm((current) => ({ ...current, new_password: event.target.value }))} /></label>
            <label className="wide-field">确认新密码<input type="password" value={changePasswordForm.confirm_password} onChange={(event) => setChangePasswordForm((current) => ({ ...current, confirm_password: event.target.value }))} /></label>
          </div>
          <FormActions submitLabel="修改密码" onCancel={closeModal} onSubmit={changePassword} disabled={!changePasswordForm.current_password || changePasswordForm.new_password.length < 6 || changePasswordForm.new_password !== changePasswordForm.confirm_password} />
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
        <AccountPoolDetailModal accountPoolDetail={accountPoolDetail} poolDirectAccountId={poolDirectAccountId} setPoolDirectAccountId={setPoolDirectAccountId} directMessageForm={directMessageForm} setDirectMessageForm={setDirectMessageForm} selectedDirectContact={selectedDirectContact} onClose={closeModal} onOpenAccountCreate={openAccountCreate} onOpenAccountDetail={openAccountDetail} onRefreshAccountPoolDetail={refreshAccountPoolDetail} onStartDirectMessageToContact={startDirectMessageToContact} onCreateDirectMessageTask={createDirectMessageTask} onOpenConfirm={openConfirm} onSetReturnAfterVerification={setReturnAfterVerification} onSetModal={ctx.setModal} accountName={accountName} />
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
          <p className="muted-line">{loginAfterCreate ? '创建后会直接发送登录验证码，并在这里完成验证码和二步密码验证。' : '添加后会继续留在弹窗内完成手机号验证码登录。'}</p>
          <FormActions submitLabel={loginAfterCreate ? '创建并登录' : '添加账号'} onCancel={closeModal} onSubmit={createAccount} disabled={!accountCreateForm.display_name.trim() || !accountCreateForm.phone_number.trim()} />
        </Modal>
      )}

      {modal?.type === 'accountLogin' && accountLoginForm.account && (
        <Modal title={`${accountLoginForm.account.display_name} 完成登录`} size="small" onClose={closeModal}>
          <div className="detail-list">
            <div><dt>账号</dt><dd>{accountLoginForm.account.phone_masked}</dd></div>
            <div><dt>当前状态</dt><dd><StatusBadge status={accountLoginForm.account.status} /></dd></div>
            <div><dt>登录方式</dt><dd>手机号验证码</dd></div>
            {accountLoginForm.flow?.code_expires_at && <div><dt>验证码有效期</dt><dd>{new Date(accountLoginForm.flow.code_expires_at).toLocaleTimeString()}</dd></div>}
          </div>
          {accountLoginForm.step === 'code' && (
            <>
              <div className="policy-grid">
                <label className="wide-field">验证码
                  <input
                    value={accountLoginForm.code}
                    onChange={(event) => setAccountLoginForm((current) => ({ ...current, code: event.target.value, error: '' }))}
                    placeholder="输入 Telegram 收到的验证码"
                    autoFocus
                  />
                </label>
              </div>
              {accountLoginForm.flow?.code_preview && <p className="muted-line">开发模式验证码：{accountLoginForm.flow.code_preview}</p>}
              {accountLoginForm.error && <p className="danger-text">{accountLoginForm.error}</p>}
              <div className="modal-actions">
                <button onClick={resendAccountLoginCode} disabled={Boolean(busy)}>重新发送验证码</button>
                <button className="primary" onClick={submitAccountLoginCode} disabled={Boolean(busy) || !accountLoginForm.code.trim()}>提交验证码</button>
              </div>
            </>
          )}
          {accountLoginForm.step === 'password' && (
            <>
              <div className="policy-grid">
                <label className="wide-field">二步验证密码
                  <input
                    type="password"
                    value={accountLoginForm.password_2fa}
                    onChange={(event) => setAccountLoginForm((current) => ({ ...current, password_2fa: event.target.value, error: '' }))}
                    placeholder="输入 Telegram 2FA 密码"
                    autoFocus
                  />
                </label>
              </div>
              {accountLoginForm.error && <p className="danger-text">{accountLoginForm.error}</p>}
              <div className="modal-actions">
                <button onClick={() => setAccountLoginForm((current) => ({ ...current, step: 'code', password_2fa: '', error: '' }))}>返回验证码</button>
                <button className="primary" onClick={submitAccountLoginPassword} disabled={Boolean(busy) || !accountLoginForm.password_2fa}>完成登录</button>
              </div>
            </>
          )}
        </Modal>
      )}

      {modal?.type === 'accountMovePool' && accountDetail && (
        <Modal title="移动账号池" size="small" onClose={() => ctx.setModal({ type: 'accountDetail' })}>
          <div className="policy-grid">
            <label>目标账号池
              <select value={accountDetail.account.pool_id ?? ''} onChange={(event) => moveCurrentAccountPool(Number(event.target.value))}>
                {accountPools.map((pool) => <option key={pool.id} value={pool.id}>{pool.name}</option>)}
              </select>
            </label>
          </div>
          <div className="modal-actions"><button onClick={() => ctx.setModal({ type: 'accountDetail' })}>返回</button></div>
        </Modal>
      )}

      {modal?.type === 'accountCloneCreate' && accountDetail && (
        <Modal title="创建账号克隆计划" size="medium" onClose={() => ctx.setModal({ type: 'accountDetail' })}>
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
          <div className="modal-actions">
            <button onClick={() => ctx.setModal({ type: returnAfterVerification })}>返回</button>
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
            <label className="checkbox-line"><input type="checkbox" checked={groupPolicy.listener_enabled} onChange={(event) => setGroupPolicy({ ...groupPolicy, listener_enabled: event.target.checked })} />启用监听续聊</label>
            <label className="checkbox-line"><input type="checkbox" checked={groupPolicy.listener_auto_reply_enabled} onChange={(event) => setGroupPolicy({ ...groupPolicy, listener_auto_reply_enabled: event.target.checked })} />监听触发后自动发送</label>
            <label>监听间隔秒<input type="number" min={30} value={groupPolicy.listener_interval_seconds} onChange={(event) => setGroupPolicy({ ...groupPolicy, listener_interval_seconds: Number(event.target.value) })} /></label>
            <label>上下文条数<input type="number" min={1} max={100} value={groupPolicy.listener_context_limit} onChange={(event) => setGroupPolicy({ ...groupPolicy, listener_context_limit: Number(event.target.value) })} /></label>
            <div className="wide-field">
              <span className="field-label">监听号</span>
              <div className="choice-grid">
                {(groupDetail?.group.id === selectedGroup.id ? groupDetail.accounts : accounts).map((account) => (
                  <label className="checkbox-line" key={account.id}>
                    <input
                      type="checkbox"
                      checked={groupPolicy.listener_account_ids.includes(account.id)}
                      onChange={(event) => {
                        const nextIds = event.target.checked
                          ? [...groupPolicy.listener_account_ids, account.id]
                          : groupPolicy.listener_account_ids.filter((id) => id !== account.id);
                        setGroupPolicy({ ...groupPolicy, listener_account_ids: Array.from(new Set(nextIds)) });
                      }}
                    />
                    {account.display_name}{account.username ? ` / @${account.username}` : ''} / {account.status}
                  </label>
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
              <label>平台备注名<input value={profileForm.display_name} onChange={(event) => setProfileForm({ ...profileForm, display_name: event.target.value })} /></label>
              <label>TG 名<input value={profileForm.tg_first_name} onChange={(event) => setProfileForm({ ...profileForm, tg_first_name: event.target.value })} /></label>
              <label>TG 姓<input value={profileForm.tg_last_name} onChange={(event) => setProfileForm({ ...profileForm, tg_last_name: event.target.value })} /></label>
              <label className="wide-field">头像上传<input type="file" accept={runtime?.avatar_allowed_types.join(',') ?? 'image/jpeg,image/png,image/webp'} onChange={(event) => setAvatarFile(event.target.files?.[0] ?? null)} /></label>
              <label className="wide-field">TG 简介<textarea value={profileForm.tg_bio} maxLength={220} onChange={(event) => setProfileForm({ ...profileForm, tg_bio: event.target.value })} /></label>
            </div>
          </div>
          <p className="muted-line">头像最大 {Math.round((runtime?.avatar_max_bytes ?? 0) / 1024 / 1024) || 2}MB；保存后会自动进入后台同步处理。</p>
          <FormActions submitLabel="保存并同步" onCancel={() => ctx.setModal({ type: 'accountDetail' })} onSubmit={saveAccountProfile} disabled={!profileForm.display_name.trim()} />
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
        <AccountDetailModal accountDetail={accountDetail} accountDetailTab={accountDetailTab} setAccountDetailTab={setAccountDetailTab} runtime={runtime} directMessageForm={directMessageForm} setDirectMessageForm={setDirectMessageForm} selectedDirectContact={selectedDirectContact} accountContacts={accountContacts} accounts={accounts} avatarUrl={avatarUrl} onClose={closeModal} onOpenAccountProfileEdit={openAccountProfileEdit} onQueueAccountSyncNow={queueAccountSyncNow} onPollVerificationCodes={pollVerificationCodes} onStartDirectMessageToContact={startDirectMessageToContact} onCreateDirectMessageTask={createDirectMessageTask} onConfirmClonePlan={confirmClonePlan} onRetryCloneItem={ctx.retryCloneItem} onRetryAccountProfileSync={retryAccountProfileSync} onDismissVerificationTask={dismissVerificationTask} onConfirmVerificationTask={confirmVerificationTask} onOpenConfirm={openConfirm} onSetReturnAfterVerification={setReturnAfterVerification} onSetModal={ctx.setModal} onSetCloneForm={setCloneForm} accountName={accountName} />
      )}

      {modal?.type === 'campaignCreate' && (
        <CampaignWizard groups={groups} aiProviders={aiProviders} materials={materials} campaignStep={campaignStep} setCampaignStep={setCampaignStep} selectedTargetGroupIds={selectedTargetGroupIds} recommendedAccounts={recommendedAccounts} selectedAccountsByGroup={selectedAccountsByGroup} targetGroupsMissingAccounts={targetGroupsMissingAccounts} topic={topic} setTopic={setTopic} sendWindow={sendWindow} setSendWindow={setSendWindow} intensity={intensity} setIntensity={setIntensity} draftCount={draftCount} setDraftCount={setDraftCount} tone={tone} setTone={setTone} selectedAiProviderId={selectedAiProviderId} setSelectedAiProviderId={setSelectedAiProviderId} selectedMaterialIds={selectedMaterialIds} jitterMinSeconds={jitterMinSeconds} setJitterMinSeconds={setJitterMinSeconds} jitterMaxSeconds={jitterMaxSeconds} setJitterMaxSeconds={setJitterMaxSeconds} batchIntervalSeconds={batchIntervalSeconds} setBatchIntervalSeconds={setBatchIntervalSeconds} respectSendWindow={respectSendWindow} setRespectSendWindow={setRespectSendWindow} onClose={closeModal} onToggleTargetGroup={toggleTargetGroup} onGoAccountStep={goCampaignAccountStep} onGoContentStep={goCampaignContentStep} onToggleRecommendedAccount={toggleRecommendedAccount} onSetGroupAccountsSelected={setGroupAccountsSelected} onToggleMaterial={toggleMaterial} onCreateCampaignAndDrafts={createCampaignAndDrafts} groupName={groupName} />
      )}

      {modal?.type === 'confirmAction' && <ConfirmDialog payload={modal.payload} onClose={closeModal} />}
      {resultDialog && <ResultDialog dialog={resultDialog} onClose={() => setResultDialog(null)} />}
    </>
  );
}
