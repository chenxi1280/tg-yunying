import type { Dispatch, SetStateAction } from 'react';
import { api } from '../../shared/api/client';
import type { ContentKeywordRule, CurrentUser, Material, MaterialImportResult, ModalState } from '../types';

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

interface ContentActionParams {
  currentUser: CurrentUser | null;
  keywordRuleForm: { id: number | null; keyword: string; match_type: string; is_active: boolean; note: string };
  materialFile: File[] | null;
  materialForm: MaterialFormState;
  setKeywordRuleForm: (form: { id: number | null; keyword: string; match_type: string; is_active: boolean; note: string }) => void;
  setMaterialFile: (file: File[] | null) => void;
  setMaterialForm: Dispatch<SetStateAction<MaterialFormState>>;
  setMaterials: (updater: (current: Material[]) => Material[]) => void;
  setModal: (modal: ModalState) => void;
  setBusy: (busy: string) => void;
  closeModal: () => void;
  refresh: () => Promise<void>;
  showResult: (title: string, detail: string) => void;
}

export function createContentActions(params: ContentActionParams) {
  async function createMaterial() {
    params.setBusy('新增素材');
    const { id: _id, ...payload } = params.materialForm;
    let material: Material;
    if (payload.source_kind === 'upload') {
      const files = params.materialFile ?? [];
      if (!files.length) throw new Error('请先选择素材文件');
      const form = new FormData();
      form.append('title', payload.title);
      form.append('material_type', payload.material_type);
      form.append('tags', payload.tags);
      form.append('caption', payload.content);
      form.append('emoji_asset_kind', payload.emoji_asset_kind);
      form.append('tenant_id', String(params.currentUser?.tenant_id ?? 1));
      const zipFiles = files.filter((file) => file.name.toLowerCase().endsWith('.zip') || file.type === 'application/zip' || file.type === 'application/x-zip-compressed');
      if (zipFiles.length) {
        if (files.length > 1 || zipFiles.length > 1) throw new Error('ZIP 导入一次只支持选择一个压缩包');
        form.append('file', zipFiles[0]);
        const result = await api<MaterialImportResult>('/materials/upload/zip', {
          method: 'POST',
          body: form,
          timeoutMs: 60_000,
        });
        params.setMaterialFile(null);
        params.closeModal();
        params.setModal({ type: 'materialImportResult', payload: result });
        await params.refresh();
        return;
      }
      if (files.length === 1) {
        form.append('file', files[0]);
        material = await api<Material>('/materials/upload', {
          method: 'POST',
          body: form,
        });
      } else {
        files.forEach((file) => form.append('files', file));
        const uploaded = await api<Material[]>('/materials/upload/batch', {
          method: 'POST',
          body: form,
        });
        if (!uploaded.length) throw new Error('批量上传未返回素材');
        material = uploaded[0];
      }
    } else {
      material = await api<Material>('/materials', {
        method: 'POST',
        body: JSON.stringify({ ...payload, tenant_id: params.currentUser?.tenant_id ?? 1 }),
      });
    }
    params.setMaterialFile(null);
    params.closeModal();
    params.showResult('素材已新增', `已新增素材：${payload.source_kind === 'upload' && (params.materialFile?.length ?? 0) > 1 ? `${params.materialFile?.length ?? 0} 个文件` : material.title}`);
    await params.refresh();
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
    params.setBusy('保存素材');
    const { id, ...payload } = params.materialForm;
    const material = await api<Material>(`/materials/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    });
    params.setMaterials((current) => current.map((item) => item.id === material.id ? material : item));
    params.closeModal();
    params.showResult('素材已保存', `已更新素材：${material.title}`);
    await params.refresh();
  }

  async function disableMaterial(material: Material) {
    params.setBusy('禁用素材');
    const updated = await api<Material>(`/materials/${material.id}/disable`, {
      method: 'POST',
      body: JSON.stringify({ reason: '素材中心手动禁用' }),
    });
    params.setMaterials((current) => current.map((item) => item.id === updated.id ? updated : item));
    params.showResult('素材已禁用', `已禁用素材：${updated.title}`);
    await params.refresh();
  }

  async function restoreMaterial(material: Material) {
    params.setBusy('恢复素材');
    const updated = await api<Material>(`/materials/${material.id}/restore`, {
      method: 'POST',
    });
    params.setMaterials((current) => current.map((item) => item.id === updated.id ? updated : item));
    params.showResult('素材已恢复', `已恢复素材：${updated.title}`);
    await params.refresh();
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
    params.setBusy('新增关键词');
    const rule = await api<ContentKeywordRule>('/content-keyword-rules', {
      method: 'POST',
      body: JSON.stringify({ ...params.keywordRuleForm, tenant_id: params.currentUser?.tenant_id ?? 1 }),
    });
    params.closeModal();
    params.showResult('关键词已新增', `已新增过滤关键词：${rule.keyword}`);
    params.setKeywordRuleForm({ id: null, keyword: '', match_type: 'contains', is_active: true, note: '' });
    await params.refresh();
  }

  async function saveContentKeywordRule() {
    if (!params.keywordRuleForm.id) return createContentKeywordRule();
    params.setBusy('保存关键词');
    const rule = await api<ContentKeywordRule>(`/content-keyword-rules/${params.keywordRuleForm.id}`, {
      method: 'PATCH',
      body: JSON.stringify(params.keywordRuleForm),
    });
    params.closeModal();
    params.showResult('关键词已保存', `已更新过滤关键词：${rule.keyword}`);
    await params.refresh();
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
