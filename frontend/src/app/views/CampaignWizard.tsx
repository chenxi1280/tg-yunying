import React from 'react';
import { Button, Card, Checkbox, Input, InputNumber, Select, Space, Steps, Typography } from 'antd';
import type { AiProvider, Group, Material, RecommendedAccount } from '../types';
import { Modal, FormActions, StatusBadge } from '../components/shared';
import { statusAccent } from '../utils';

interface Props {
  groups: Group[];
  aiProviders: AiProvider[];
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
  selectedAiProviderId: number | '';
  setSelectedAiProviderId: (id: number | '') => void;
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
  aiProviders,
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
  selectedAiProviderId,
  setSelectedAiProviderId,
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
  const stepItems = ['选择群聊', '选择账号', '任务内容'].map((title) => ({ title }));

  return (
    <Modal title="创建群活跃任务" size="large" onClose={onClose}>
      <Steps
        className="wizard-steps"
        current={campaignStep - 1}
        items={stepItems}
        onChange={(next) => {
          const step = next + 1;
          if (step === 1 || (step === 2 && selectedTargetGroupIds.length) || (step === 3 && !targetGroupsMissingAccounts.length && selectedTargetGroupIds.length)) {
            setCampaignStep(step);
          }
        }}
      />

      {campaignStep === 1 && (
        <Card className="wizard-panel" title="选择群聊" extra={<Typography.Text type="secondary">可以一次选择多个群。只读、禁止操作或未授权群会显示原因，后续不会进入发送。</Typography.Text>}>
          <div className="group-option-grid">
            {groups.map((group) => {
              const selectable = group.auth_status === '已授权运营' && group.can_send;
              const selected = selectedTargetGroupIds.includes(group.id);
              return (
                <Button key={group.id} className={selected ? 'selected group-option' : 'group-option'} disabled={!selectable} onClick={() => onToggleTargetGroup(group.id)}>
                  <strong>{group.title}</strong>
                  <span className="inline-status">{group.group_type} / 成员 {group.member_count} <StatusBadge status={group.auth_status} /></span>
                  <small>{selectable ? '可进入下一步选择参与账号' : `不可选：${group.auth_status}${group.can_send ? '' : ' / 群不可发言'}`}</small>
                </Button>
              );
            })}
          </div>
          {!selectedTargetGroupIds.length && <p className="muted-line">先选群聊，下一步再选择每个群里参与接话的账号。</p>}
          <FormActions submitLabel="下一步：选择账号" onCancel={onClose} onSubmit={onGoAccountStep} disabled={!selectedTargetGroupIds.length} />
        </Card>
      )}

      {campaignStep === 2 && (
        <Card className="wizard-panel" title="选择可参与账号" extra={<Typography.Text type="secondary">系统先按群权限、在线状态、健康分、冷却和失败率推荐，操作员可以全选、清空或手动调整。</Typography.Text>}>
          {selectedTargetGroupIds.map((groupId) => {
            const group = groups.find((item) => item.id === groupId);
            const rows = recommendedAccounts.filter((item) => item.group_id === groupId);
            const selectedIds = selectedAccountsByGroup[String(groupId)] ?? [];
            const selectableRows = rows.filter((row) => row.is_selectable ?? row.can_send);
            return (
              <Card className="sub-panel compact-panel" key={groupId} size="small" title={group?.title ?? `群 ${groupId}`} extra={<Space><Typography.Text type="secondary">已选 {selectedIds.length} 个 / 可用 {selectableRows.length} 个账号</Typography.Text><Button size="small" onClick={() => onSetGroupAccountsSelected(groupId, selectableRows.map((row) => row.account_id))}>全选可用</Button><Button size="small" onClick={() => onSetGroupAccountsSelected(groupId, [])}>清空</Button></Space>}>
                <div className="account-pick-grid">
                  {rows.map((item) => {
                    const selectable = item.is_selectable ?? item.can_send;
                    return (
                      <Button key={`${item.group_id}-${item.account_id}`} className={selectedIds.includes(item.account_id) ? 'selected account-pick' : 'account-pick'} disabled={!selectable} onClick={() => onToggleRecommendedAccount(item.group_id, item.account_id)}>
                        <strong>{item.account_name}</strong>
                        <span>{item.recommended ? '系统推荐' : '备选'} / 健康分 {item.health_score}</span>
                        <small>{item.cooldown_until ? `冷却到 ${new Date(item.cooldown_until).toLocaleTimeString()} / ` : ''}{selectable ? item.reason : item.unavailable_reason ?? item.reason}</small>
                      </Button>
                    );
                  })}
                </div>
                {!rows.length && <p className="danger-text">这个群下还没有同步到可参与账号，请先在账号详情同步群聊。</p>}
                {rows.length > 0 && !selectedIds.length && <p className="danger-text">请为「{group?.title ?? groupId}」至少选择一个可发送账号。</p>}
              </Card>
            );
          })}
          <Space className="modal-actions">
            <Button onClick={() => setCampaignStep(1)}>上一步</Button>
            <Button type="primary" disabled={targetGroupsMissingAccounts.length > 0} onClick={onGoContentStep}>下一步：配置内容</Button>
          </Space>
        </Card>
      )}

      {campaignStep === 3 && (
        <Card className="wizard-panel">
          <div className="wizard-summary">
            <span>目标群 {selectedTargetGroupIds.length} 个</span>
            <span>参与账号 {Object.values(selectedAccountsByGroup).reduce((total, ids) => total + ids.length, 0)} 个</span>
            <span>默认生成多账号对话脚本</span>
          </div>
          <div className="policy-grid">
            <label>强度<Select value={intensity} onChange={setIntensity} options={['轻度', '中度', '高频'].map((value) => ({ value, label: value }))} /></label>
            <label>时间窗<Input value={sendWindow} onChange={(event) => setSendWindow(event.target.value)} /></label>
            <label>草稿轮数<InputNumber min={1} max={12} value={draftCount} onChange={(value) => setDraftCount(Number(value ?? 1))} /></label>
            <label>模型后台<Select value={selectedAiProviderId || ''} onChange={(value) => setSelectedAiProviderId(Number(value) || '')} options={[{ value: '', label: '使用客户默认' }, ...aiProviders.map((provider) => ({ value: provider.id, label: `${provider.provider_name} / ${provider.model_name}` }))]} /></label>
            <label className="wide-field">话题/运营目标<Input.TextArea value={topic} onChange={(event) => setTopic(event.target.value)} /></label>
            <label className="wide-field">语气<Input.TextArea value={tone} onChange={(event) => setTone(event.target.value)} /></label>
            <label>最小抖动秒<InputNumber min={0} value={jitterMinSeconds} onChange={(value) => setJitterMinSeconds(Number(value ?? 0))} /></label>
            <label>最大抖动秒<InputNumber min={0} value={jitterMaxSeconds} onChange={(value) => setJitterMaxSeconds(Number(value ?? 0))} /></label>
            <label>批次间隔秒<InputNumber min={0} value={batchIntervalSeconds} onChange={(value) => setBatchIntervalSeconds(Number(value ?? 0))} /></label>
            <Checkbox checked={respectSendWindow} onChange={(event) => setRespectSendWindow(event.target.checked)}>遵守时间窗</Checkbox>
            <div className="wide-field">
              <span className="field-title">素材/表情包</span>
              <Space className="material-picker" wrap>
                {materials.map((material) => (
                  <Button key={material.id} type={selectedMaterialIds.includes(material.id) ? 'primary' : 'default'} onClick={() => onToggleMaterial(material.id)}>
                    {material.material_type} / {material.title}
                  </Button>
                ))}
              </Space>
            </div>
          </div>
          <p className="muted-line">系统提示词会自动决定是否调用 AI，并默认按多账号对话脚本生成接话内容；审核后才会按顺序和抖动进入待发送状态。</p>
          {targetGroupsMissingAccounts.length > 0 && <p className="danger-text">这些群还没有选择账号：{targetGroupsMissingAccounts.map((groupId) => groupName(groupId)).join('、')}</p>}
          <Space className="modal-actions">
            <Button onClick={() => setCampaignStep(2)}>上一步</Button>
            <Button type="primary" disabled={!selectedTargetGroupIds.length || !topic || targetGroupsMissingAccounts.length > 0} onClick={onCreateCampaignAndDrafts}>创建并生成草稿</Button>
          </Space>
        </Card>
      )}
    </Modal>
  );
}
