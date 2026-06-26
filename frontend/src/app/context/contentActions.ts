import type React from 'react';
import type { Dispatch, SetStateAction } from 'react';
import { api } from '../../shared/api/client';
import type { ContentKeywordRule, CurrentUser, Material, MaterialImportResult, ModalState } from '../types';

const MATERIAL_UPLOAD_TIMEOUT_MS = 60_000;
const ZIP_MIME_TYPES = new Set(['application/zip', 'application/x-zip-compressed']);

type MaterialFormState = {
  id: number | null;
  title: string;
  material_type: string;
  content: string;
  tags: string;
  emoji_asset_kind: string;
  cache_ready_status: string;
  delivery_mode: string;
  source_kind: string;
};

type MaterialPayload = Omit<MaterialFormState, 'id'>;
type MaterialCreateResult =
  | { kind: 'import'; result: MaterialImportResult }
  | { kind: 'material'; material: Material; detail: string };

interface ContentActionParams {
  currentUser: CurrentUser | null;
  keywordRuleForm: { id: number | null; keyword: string; match_type: string; is_active: boolean; note: string };
  materialFile: File[] | null;
  materialForm: MaterialFormState;
  materialSaveRequestRef: React.MutableRefObject<{ seq: number; materialId: number | null; signature: string }>;
  materialActionRequestRef: React.MutableRefObject<{ seq: number; materialId: number | null; action: string }>;
  keywordRuleSaveRequestRef: React.MutableRefObject<{ seq: number; ruleId: number | null; signature: string }>;
  setKeywordRuleForm: (form: { id: number | null; keyword: string; match_type: string; is_active: boolean; note: string }) => void;
  setMaterialFile: (file: File[] | null) => void;
  setMaterialForm: Dispatch<SetStateAction<MaterialFormState>>;
  setMaterials: (updater: (current: Material[]) => Material[]) => void;
  setModal: (modal: ModalState) => void;
  setBusy: (busy: string) => void;
  closeModal: () => void;
  refresh: () => Promise<void>;
  handleActionError: (error: unknown) => void;
  showResult: (title: string, detail: string) => void;
}

function isZipFile(file: File) {
  return file.name.toLowerCase().endsWith('.zip') || ZIP_MIME_TYPES.has(file.type);
}

function createMaterialForm(payload: MaterialPayload, tenantId: number) {
  const form = new FormData();
  form.append('title', payload.title);
  form.append('material_type', payload.material_type);
  form.append('tags', payload.tags);
  form.append('caption', payload.content);
  form.append('emoji_asset_kind', payload.emoji_asset_kind);
  form.append('tenant_id', String(tenantId));
  return form;
}

function errorText(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}

async function uploadZipMaterial(files: File[], zipFiles: File[], form: FormData): Promise<MaterialCreateResult> {
  if (files.length > 1 || zipFiles.length > 1) throw new Error('ZIP 导入一次只支持选择一个压缩包');
  form.append('file', zipFiles[0]);
  const result = await api<MaterialImportResult>('/materials/upload/zip', {
    method: 'POST',
    body: form,
    timeoutMs: MATERIAL_UPLOAD_TIMEOUT_MS,
  });
  return { kind: 'import', result };
}

async function uploadMaterialFiles(payload: MaterialPayload, files: File[], tenantId: number): Promise<MaterialCreateResult> {
  if (!files.length) throw new Error('请先选择素材文件');
  const form = createMaterialForm(payload, tenantId);
  const zipFiles = files.filter(isZipFile);
  if (zipFiles.length) return uploadZipMaterial(files, zipFiles, form);
  if (files.length === 1) {
    form.append('file', files[0]);
    const material = await api<Material>('/materials/upload', { method: 'POST', body: form });
    return { kind: 'material', material, detail: material.title };
  }
  files.forEach((file) => form.append('files', file));
  const uploaded = await api<Material[]>('/materials/upload/batch', { method: 'POST', body: form });
  if (!uploaded.length) throw new Error('批量上传未返回素材');
  return { kind: 'material', material: uploaded[0], detail: `${files.length} 个文件` };
}

async function createMaterialRecord(payload: MaterialPayload, tenantId: number): Promise<MaterialCreateResult> {
  const material = await api<Material>('/materials', {
    method: 'POST',
    body: JSON.stringify({ ...payload, tenant_id: tenantId }),
  });
  return { kind: 'material', material, detail: material.title };
}

export function createContentActions(params: ContentActionParams) {
  async function refreshContentResourcesAfterAction(actionLabel: string) {
    try {
      await params.refresh();
    } catch (error) {
      params.showResult('素材中心数据刷新失败', `${actionLabel}操作已完成，但刷新素材中心数据失败：${errorText(error)}`);
    }
  }

  function materialPayload() {
    const { id: _id, ...payload } = params.materialForm;
    return payload;
  }

  function materialFilesSignature(files: File[]) {
    return files.map((file) => ({
      name: file.name,
      size: file.size,
      type: file.type,
      lastModified: file.lastModified,
    }));
  }

  function materialSavePayloadSignature(materialId: number | null, tenantId: number, payload: MaterialPayload, files: File[]) {
    return JSON.stringify({ materialId, tenantId, payload, files: materialFilesSignature(files) });
  }

  function beginMaterialSaveRequest(materialId: number | null, signature: string) {
    const requestSeq = params.materialSaveRequestRef.current.seq + 1;
    params.materialSaveRequestRef.current = { seq: requestSeq, materialId, signature };
    return requestSeq;
  }

  function isCurrentMaterialSaveRequest(requestSeq: number) {
    return params.materialSaveRequestRef.current.seq === requestSeq;
  }

  function isActiveMaterialSaveRequest(materialId: number | null, requestSeq: number, signature: string) {
    const tenantId = params.currentUser?.tenant_id ?? 1;
    return isCurrentMaterialSaveRequest(requestSeq)
      && params.materialSaveRequestRef.current.materialId === materialId
      && params.materialSaveRequestRef.current.signature === signature
      && params.materialForm.id === materialId
      && materialSavePayloadSignature(materialId, tenantId, materialPayload(), params.materialFile ?? []) === signature;
  }

  function beginMaterialActionRequest(materialId: number, action: string) {
    const requestSeq = params.materialActionRequestRef.current.seq + 1;
    params.materialActionRequestRef.current = { seq: requestSeq, materialId, action };
    return requestSeq;
  }

  function isCurrentMaterialActionRequest(requestSeq: number) {
    return params.materialActionRequestRef.current.seq === requestSeq;
  }

  function isActiveMaterialActionRequest(materialId: number, action: string, requestSeq: number) {
    return isCurrentMaterialActionRequest(requestSeq)
      && params.materialActionRequestRef.current.materialId === materialId
      && params.materialActionRequestRef.current.action === action;
  }

  function keywordRulePayload() {
    return { ...params.keywordRuleForm };
  }

  function keywordRulePayloadSignature(ruleId: number | null, tenantId: number, payload: ReturnType<typeof keywordRulePayload>) {
    return JSON.stringify({ ruleId, tenantId, payload });
  }

  function beginKeywordRuleSaveRequest(ruleId: number | null, signature: string) {
    const requestSeq = params.keywordRuleSaveRequestRef.current.seq + 1;
    params.keywordRuleSaveRequestRef.current = { seq: requestSeq, ruleId, signature };
    return requestSeq;
  }

  function isCurrentKeywordRuleSaveRequest(requestSeq: number) {
    return params.keywordRuleSaveRequestRef.current.seq === requestSeq;
  }

  function isActiveKeywordRuleSaveRequest(ruleId: number | null, requestSeq: number, signature: string) {
    const tenantId = params.currentUser?.tenant_id ?? 1;
    return isCurrentKeywordRuleSaveRequest(requestSeq)
      && params.keywordRuleSaveRequestRef.current.ruleId === ruleId
      && params.keywordRuleSaveRequestRef.current.signature === signature
      && params.keywordRuleForm.id === ruleId
      && keywordRulePayloadSignature(ruleId, tenantId, keywordRulePayload()) === signature;
  }

  async function createMaterial() {
    const materialId = null;
    const payload = materialPayload();
    const tenantId = params.currentUser?.tenant_id ?? 1;
    const files = params.materialFile ?? [];
    const signature = materialSavePayloadSignature(materialId, tenantId, payload, files);
    const requestSeq = beginMaterialSaveRequest(materialId, signature);
    params.setBusy('新增素材');
    try {
      const result = payload.source_kind === 'upload'
        ? await uploadMaterialFiles(payload, files, tenantId)
        : await createMaterialRecord(payload, tenantId);
      if (!isActiveMaterialSaveRequest(materialId, requestSeq, signature)) return;
      params.setMaterialFile(null);
      params.closeModal();
      if (result.kind === 'import') {
        params.setModal({ type: 'materialImportResult', payload: result.result });
      } else {
        params.showResult('素材已新增', `已新增素材：${result.detail}`);
      }
      await refreshContentResourcesAfterAction(result.kind === 'import' ? 'ZIP 导入' : '素材新增');
    } catch (error) {
      if (!isActiveMaterialSaveRequest(materialId, requestSeq, signature)) return;
      params.handleActionError(error);
    } finally {
      if (isCurrentMaterialSaveRequest(requestSeq)) params.setBusy('');
    }
  }

  function openMaterialEdit(material: Material) {
    params.setMaterialForm({
      id: material.id,
      title: material.title,
      material_type: material.material_type,
      content: material.content,
      tags: material.tags,
      emoji_asset_kind: material.emoji_asset_kind || (material.material_type === '表情包' ? 'image_meme' : ''),
      cache_ready_status: material.cache_ready_status || 'not_cached',
      delivery_mode: material.delivery_mode || 'download_reupload',
      source_kind: material.source_kind || 'url',
    });
    params.setMaterialFile(null);
    params.setModal({ type: 'materialEdit' });
  }

  async function saveMaterial() {
    if (!params.materialForm.id) return createMaterial();
    const materialId = params.materialForm.id;
    const payload = materialPayload();
    const tenantId = params.currentUser?.tenant_id ?? 1;
    const files = params.materialFile ?? [];
    const signature = materialSavePayloadSignature(materialId, tenantId, payload, files);
    const requestSeq = beginMaterialSaveRequest(materialId, signature);
    params.setBusy('保存素材');
    try {
      const material = await api<Material>(`/materials/${materialId}`, {
        method: 'PATCH',
        body: JSON.stringify(payload),
      });
      if (!isActiveMaterialSaveRequest(materialId, requestSeq, signature)) return;
      params.setMaterials((current) => current.map((item) => item.id === material.id ? material : item));
      params.closeModal();
      params.showResult('素材已保存', `已更新素材：${material.title}`);
      await refreshContentResourcesAfterAction('素材保存');
    } catch (error) {
      if (!isActiveMaterialSaveRequest(materialId, requestSeq, signature)) return;
      params.handleActionError(error);
    } finally {
      if (isCurrentMaterialSaveRequest(requestSeq)) params.setBusy('');
    }
  }

  async function disableMaterial(material: Material) {
    const action = 'disable';
    const requestSeq = beginMaterialActionRequest(material.id, action);
    params.setBusy('禁用素材');
    try {
      const updated = await api<Material>(`/materials/${material.id}/disable`, {
        method: 'POST',
        body: JSON.stringify({ reason: '素材中心手动禁用' }),
      });
      if (!isActiveMaterialActionRequest(material.id, action, requestSeq)) return;
      params.setMaterials((current) => current.map((item) => item.id === updated.id ? updated : item));
      params.showResult('素材已禁用', `已禁用素材：${updated.title}`);
      await refreshContentResourcesAfterAction('素材禁用');
    } catch (error) {
      if (!isActiveMaterialActionRequest(material.id, action, requestSeq)) return;
      params.handleActionError(error);
    } finally {
      if (isCurrentMaterialActionRequest(requestSeq)) params.setBusy('');
    }
  }

  async function restoreMaterial(material: Material) {
    const action = 'restore';
    const requestSeq = beginMaterialActionRequest(material.id, action);
    params.setBusy('恢复素材');
    try {
      const updated = await api<Material>(`/materials/${material.id}/restore`, {
        method: 'POST',
      });
      if (!isActiveMaterialActionRequest(material.id, action, requestSeq)) return;
      params.setMaterials((current) => current.map((item) => item.id === updated.id ? updated : item));
      params.showResult('素材已恢复', `已恢复素材：${updated.title}`);
      await refreshContentResourcesAfterAction('素材恢复');
    } catch (error) {
      if (!isActiveMaterialActionRequest(material.id, action, requestSeq)) return;
      params.handleActionError(error);
    } finally {
      if (isCurrentMaterialActionRequest(requestSeq)) params.setBusy('');
    }
  }

  function openContentKeywordRuleEdit(rule: ContentKeywordRule) {
    params.setKeywordRuleForm({
      id: rule.id,
      keyword: rule.keyword,
      match_type: rule.match_type,
      is_active: rule.is_active,
      note: rule.note,
    });
    params.setModal({ type: 'keywordRuleEdit' });
  }

  async function createContentKeywordRule() {
    const ruleId = null;
    const tenantId = params.currentUser?.tenant_id ?? 1;
    const payload = keywordRulePayload();
    const signature = keywordRulePayloadSignature(ruleId, tenantId, payload);
    const requestSeq = beginKeywordRuleSaveRequest(ruleId, signature);
    params.setBusy('新增关键词');
    try {
      const rule = await api<ContentKeywordRule>('/content-keyword-rules', {
        method: 'POST',
        body: JSON.stringify({ ...payload, tenant_id: tenantId }),
      });
      if (!isActiveKeywordRuleSaveRequest(ruleId, requestSeq, signature)) return;
      params.closeModal();
      params.showResult('关键词已新增', `已新增过滤关键词：${rule.keyword}`);
      params.setKeywordRuleForm({ id: null, keyword: '', match_type: 'contains', is_active: true, note: '' });
      await refreshContentResourcesAfterAction('关键词新增');
    } catch (error) {
      if (!isActiveKeywordRuleSaveRequest(ruleId, requestSeq, signature)) return;
      params.handleActionError(error);
    } finally {
      if (isCurrentKeywordRuleSaveRequest(requestSeq)) params.setBusy('');
    }
  }

  async function saveContentKeywordRule() {
    if (!params.keywordRuleForm.id) return createContentKeywordRule();
    const ruleId = params.keywordRuleForm.id;
    const tenantId = params.currentUser?.tenant_id ?? 1;
    const payload = keywordRulePayload();
    const signature = keywordRulePayloadSignature(ruleId, tenantId, payload);
    const requestSeq = beginKeywordRuleSaveRequest(ruleId, signature);
    params.setBusy('保存关键词');
    try {
      const rule = await api<ContentKeywordRule>(`/content-keyword-rules/${ruleId}`, {
        method: 'PATCH',
        body: JSON.stringify(payload),
      });
      if (!isActiveKeywordRuleSaveRequest(ruleId, requestSeq, signature)) return;
      params.closeModal();
      params.showResult('关键词已保存', `已更新过滤关键词：${rule.keyword}`);
      await refreshContentResourcesAfterAction('关键词保存');
    } catch (error) {
      if (!isActiveKeywordRuleSaveRequest(ruleId, requestSeq, signature)) return;
      params.handleActionError(error);
    } finally {
      if (isCurrentKeywordRuleSaveRequest(requestSeq)) params.setBusy('');
    }
  }

  return {
    createMaterial,
    disableMaterial,
    openMaterialEdit,
    restoreMaterial,
    saveMaterial,
    createContentKeywordRule,
    openContentKeywordRuleEdit,
    saveContentKeywordRule,
  };
}
