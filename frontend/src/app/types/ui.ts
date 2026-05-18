import type { VerificationTask } from './accounts';

export type ConfirmPayload = {
  title: string;
  message: string;
  confirmLabel?: string;
  tone?: 'normal' | 'danger';
  restoreModalType?: 'accountDetail' | 'accountPoolDetail';
  onConfirm: () => void | Promise<void>;
};

export type ModalState =
  | { type: 'accountCreate' }
  | { type: 'accountLogin' }
  | { type: 'accountPoolCreate' }
  | { type: 'accountPoolDetail' }
  | { type: 'accountMovePool' }
  | { type: 'accountCloneCreate' }
  | { type: 'verificationTaskDetail'; payload: VerificationTask }
  | { type: 'developerAppCreate' }
  | { type: 'developerAppEdit' }
  | { type: 'tenantEdit' }
  | { type: 'adminUserEdit' }
  | { type: 'aiProviderCreate' }
  | { type: 'aiProviderEdit' }
  | { type: 'promptTemplateCreate' }
  | { type: 'promptTemplateEdit' }
  | { type: 'materialCreate' }
  | { type: 'materialEdit' }
  | { type: 'keywordRuleCreate' }
  | { type: 'keywordRuleEdit' }
  | { type: 'tenantAiEdit' }
  | { type: 'changePassword' }
  | { type: 'groupPolicyEdit' }
  | { type: 'accountDetail' }
  | { type: 'groupDetail' }
  | { type: 'draftEdit' }
  | { type: 'accountProfileEdit' }
  | null;

export type BadgeTone = 'positive' | 'warning' | 'danger' | 'neutral' | 'muted';
