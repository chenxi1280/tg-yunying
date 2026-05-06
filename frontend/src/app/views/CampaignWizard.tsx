import React from 'react';
import type { Group, Material, RecommendedAccount } from '../types';
import { Modal, FormActions, StatusBadge } from '../components/shared';
import { statusAccent } from '../utils';

interface Props {
  groups: Group[];
  materials: Material[];
  campaignStep: number;
  setCampaignStep: (step: number) => void;
  selectedTargetGroupIds: number[];
  recommendedAccounts: RecommendedAccount[];
  selectedAccountsByGroup: Record<string, number[]>;
  targetGroupsMissingAccounts: number[];
  topic: string;
  setTopic: (topic: string) => void;
  sendWindow: string;
  setSendWindow: (window: string) => void;
  intensity: string;
  setIntensity: (intensity: string) => void;
  draftCount: number;
  setDraftCount: (count: number) => void;
  tone: string;
  setTone: (tone: string) => void;
  selectedMaterialIds: number[];
  jitterMinSeconds: number;
  setJitterMinSeconds: (seconds: number) => void;
  jitterMaxSeconds: number;
  setJitterMaxSeconds: (seconds: number) => void;
  batchIntervalSeconds: number;
  setBatchIntervalSeconds: (seconds: number) => void;
  respectSendWindow: boolean;
  setRespectSendWindow: (respect: boolean) => void;
  onClose: () => void;
  onToggleTargetGroup: (groupId: number) => void;
  onGoAccountStep: () => Promise<void>;
  onGoContentStep: () => void;
  onToggleRecommendedAccount: (groupId: number, accountId: number) => void;
  onSetGroupAccountsSelected: (groupId: number, accountIds: number[]) => void;
  onToggleMaterial: (materialId: number) => void;
  onCreateCampaignAndDrafts: () => Promise<void>;
  groupName: (groupId: number | null | undefined) => string;
}

export default function CampaignWizard({
  groups,
  materials,
  campaignStep,
  setCampaignStep,
  selectedTargetGroupIds,
  recommendedAccounts,
  selectedAccountsByGroup,
  targetGroupsMissingAccounts,
  topic,
  setTopic,
  sendWindow,
  setSendWindow,
  intensity,
  setIntensity,
  draftCount,
  setDraftCount,
  tone,
  setTone,
  selectedMaterialIds,
  jitterMinSeconds,
  setJitterMinSeconds,
  jitterMaxSeconds,
  setJitterMaxSeconds,
  batchIntervalSeconds,
  setBatchIntervalSeconds,
  respectSendWindow,
  setRespectSendWindow,
  onClose,
  onToggleTargetGroup,
  onGoAccountStep,
  onGoContentStep,
  onToggleRecommendedAccount,
  onSetGroupAccountsSelected,
  onToggleMaterial,
  onCreateCampaignAndDrafts,
  groupName,
}: Props) {
  return (
    <Modal title="创建群活跃任务" size="large" onClose={onClose}>
      <div className="wizard-steps">
        {['选择群聊', '选择账号', '任务内容'].map((label, index) => (
          <button key={label} className={campaignStep === index + 1 ? 'active' : campaignStep > index + 1 ? 'done' : ''} type="button" onClick={() => {
            if (index + 1 === 1 || (index + 1 === 2 && selectedTargetGroupIds.length) || (index + 1 === 3 && !targetGroupsMissingAccounts.length && selectedTargetGroupIds.length)) {
              setCampaignStep(index + 1);
            }
          }}>
            <span>{index + 1}</span>{label}
          </button>
        ))}
      </div>

      {campaignStep === 1 && (
        <div className="wizard-panel">
          <div className="section-title">
            <div>
              <h2>选择群聊</h2>
              <span>可以一次选择多个群。只读、禁止操作或未授权群会显示原因，后续不会进入发送。</span>
            </div>
          </div>
          <div className="group-option-grid">
            {groups.map((group) => {
              const selectable = group.auth_status === '已授权运营' && group.can_send;
              const selected = selectedTargetGroupIds.includes(group.id);
              return (
                <button key={group.id} type="button" className={selected ? 'selected group-option' : 'group-option'} disabled={!selectable} onClick={() => onToggleTargetGroup(group.id)}>
                  <strong>{group.title}</strong>
                  <span className="inline-status">{group.group_type} / 成员 {group.member_count} <StatusBadge status={group.auth_status} /></span>
                  <small>{selectable ? '可进入下一步选择参与账号' : `不可选：${group.auth_status}${group.can_send ? '' : ' / 群不可发言'}`}</small>
                </button>
              );
            })}
          </div>
          {!selectedTargetGroupIds.length && <p className="muted-line">先选群聊，下一步再选择每个群里参与接话的账号。</p>}
          <FormActions submitLabel="下一步：选择账号" onCancel={onClose} onSubmit={onGoAccountStep} disabled={!selectedTargetGroupIds.length} />
        </div>
      )}

      {campaignStep === 2 && (
        <div className="wizard-panel">
          <div className="section-title">
            <div>
              <h2>选择可参与账号</h2>
              <span>系统先按群权限、在线状态、健康分、冷却和失败率推荐，操作员可以全选、清空或手动调整。</span>
            </div>
          </div>
          {selectedTargetGroupIds.map((groupId) => {
            const group = groups.find((item) => item.id === groupId);
            const rows = recommendedAccounts.filter((item) => item.group_id === groupId);
            const selectedIds = selectedAccountsByGroup[String(groupId)] ?? [];
            const selectableRows = rows.filter((row) => row.is_selectable ?? row.can_send);
            return (
              <div className="sub-panel compact-panel" key={groupId}>
                <div className="section-title">
                  <div>
                    <h2>{group?.title ?? `群 ${groupId}`}</h2>
                    <span>已选 {selectedIds.length} 个 / 可用 {selectableRows.length} 个账号</span>
                  </div>
                  <div className="row-actions">
                    <button className="small" onClick={() => onSetGroupAccountsSelected(groupId, selectableRows.map((row) => row.account_id))}>全选可用</button>
                    <button className="small" onClick={() => onSetGroupAccountsSelected(groupId, [])}>清空</button>
                  </div>
                </div>
                <div className="account-pick-grid">
                  {rows.map((item) => {
                    const selectable = item.is_selectable ?? item.can_send;
                    return (
                      <button key={`${item.group_id}-${item.account_id}`} type="button" className={selectedIds.includes(item.account_id) ? 'selected account-pick' : 'account-pick'} disabled={!selectable} onClick={() => onToggleRecommendedAccount(item.group_id, item.account_id)}>
                        <strong>{item.account_name}</strong>
                        <span>{item.recommended ? '系统推荐' : '备选'} / 健康分 {item.health_score}</span>
                        <small>{item.cooldown_until ? `冷却到 ${new Date(item.cooldown_until).toLocaleTimeString()} / ` : ''}{selectable ? item.reason : item.unavailable_reason ?? item.reason}</small>
                      </button>
                    );
                  })}
                </div>
                {!rows.length && <p className="danger-text">这个群下还没有同步到可参与账号，请先在账号详情同步群聊。</p>}
                {rows.length > 0 && !selectedIds.length && <p className="danger-text">请为「{group?.title ?? groupId}」至少选择一个可发送账号。</p>}
              </div>
            );
          })}
          <div className="modal-actions">
            <button type="button" onClick={() => setCampaignStep(1)}>上一步</button>
            <button className="primary" type="button" disabled={targetGroupsMissingAccounts.length > 0} onClick={onGoContentStep}>下一步：配置内容</button>
          </div>
        </div>
      )}

      {campaignStep === 3 && (
        <div className="wizard-panel">
          <div className="wizard-summary">
            <span>目标群 {selectedTargetGroupIds.length} 个</span>
            <span>参与账号 {Object.values(selectedAccountsByGroup).reduce((total, ids) => total + ids.length, 0)} 个</span>
            <span>默认生成多账号对话脚本</span>
          </div>
          <div className="policy-grid">
            <label>强度
              <select value={intensity} onChange={(event) => setIntensity(event.target.value)}>
                <option>轻度</option>
                <option>中度</option>
                <option>高频</option>
              </select>
            </label>
            <label>时间窗<input value={sendWindow} onChange={(event) => setSendWindow(event.target.value)} /></label>
            <label>草稿轮数<input type="number" min={1} max={12} value={draftCount} onChange={(event) => setDraftCount(Number(event.target.value))} /></label>
            <label className="wide-field">话题/运营目标<textarea value={topic} onChange={(event) => setTopic(event.target.value)} /></label>
            <label className="wide-field">语气<textarea value={tone} onChange={(event) => setTone(event.target.value)} /></label>
            <label>最小抖动秒<input type="number" min={0} value={jitterMinSeconds} onChange={(event) => setJitterMinSeconds(Number(event.target.value))} /></label>
            <label>最大抖动秒<input type="number" min={0} value={jitterMaxSeconds} onChange={(event) => setJitterMaxSeconds(Number(event.target.value))} /></label>
            <label>批次间隔秒<input type="number" min={0} value={batchIntervalSeconds} onChange={(event) => setBatchIntervalSeconds(Number(event.target.value))} /></label>
            <label className="checkbox-line"><input type="checkbox" checked={respectSendWindow} onChange={(event) => setRespectSendWindow(event.target.checked)} />遵守时间窗</label>
            <div className="wide-field">
              <span className="field-title">素材/表情包</span>
              <div className="material-picker">
                {materials.map((material) => (
                  <button key={material.id} type="button" className={selectedMaterialIds.includes(material.id) ? 'selected' : ''} onClick={() => onToggleMaterial(material.id)}>
                    {material.material_type} / {material.title}
                  </button>
                ))}
              </div>
            </div>
          </div>
          <p className="muted-line">系统提示词会自动决定是否调用 AI，并默认按多账号对话脚本生成接话内容；审核后才会按顺序和抖动进入待发送状态。</p>
          {targetGroupsMissingAccounts.length > 0 && <p className="danger-text">这些群还没有选择账号：{targetGroupsMissingAccounts.map((groupId) => groupName(groupId)).join('、')}</p>}
          <div className="modal-actions">
            <button type="button" onClick={() => setCampaignStep(2)}>上一步</button>
            <button className="primary" type="button" disabled={!selectedTargetGroupIds.length || !topic || targetGroupsMissingAccounts.length > 0} onClick={onCreateCampaignAndDrafts}>创建并生成草稿</button>
          </div>
        </div>
      )}
    </Modal>
  );
}
