import React from 'react';
import type { AiProvider, PromptTemplate, TenantAiSetting, SchedulingSetting, Material } from '../types';
import { StatusBadge, Badge } from '../components/shared';
import { statusAccent } from '../utils';

interface Props {
  aiProviders: AiProvider[];
  promptTemplates: PromptTemplate[];
  tenantAiSetting: TenantAiSetting | null;
  schedulingSetting: SchedulingSetting | null;
  materials: Material[];
  currentUserRole: string | undefined;
  onCreateProvider: () => void;
  onEditProvider: (provider: AiProvider) => void;
  onToggleProvider: (provider: AiProvider) => void;
  onCheckProvider: (provider: AiProvider) => void;
  onEditTenantAi: () => void;
  onEditScheduling: () => void;
  onCreatePromptTemplate: () => void;
  onCreateMaterial: () => void;
}

export default function AISettingsView({
  aiProviders,
  promptTemplates,
  tenantAiSetting,
  schedulingSetting,
  materials,
  currentUserRole,
  onCreateProvider,
  onEditProvider,
  onToggleProvider,
  onCheckProvider,
  onEditTenantAi,
  onEditScheduling,
  onCreatePromptTemplate,
  onCreateMaterial,
}: Props) {
  return (
    <section className="view-grid">
      <section className="panel">
        <div className="section-title">
          <div>
            <h2>AI 供应商</h2>
            <span>MiMo / DeepSeek 使用 OpenAI-Compatible 接口，Key 加密保存</span>
          </div>
          {currentUserRole === '系统管理员' && <button className="primary" onClick={onCreateProvider}>新增供应商</button>}
        </div>
        <div className="cards-grid developer-grid">
          {!aiProviders.length && (
            <article className="developer-card status-accent warning">
              <h3>还没有 AI 供应商</h3>
              <p>请配置真实 OpenAI-Compatible Base URL、模型名和 API Key 后再启用 AI 草稿。</p>
              {currentUserRole === '系统管理员' && <button className="primary" onClick={onCreateProvider}>新增供应商</button>}
            </article>
          )}
          {aiProviders.map((provider) => (
            <article className={`developer-card ${statusAccent(provider.is_active ? provider.health_status : '禁用')}`} key={provider.id}>
              <div>
                <StatusBadge status={provider.is_active ? provider.health_status : '禁用'} />
                <Badge tone="neutral">{provider.provider_type}</Badge>
              </div>
              <h3>{provider.provider_name}</h3>
              <p>{provider.model_name}</p>
              <p>{provider.base_url}</p>
              {provider.last_error && <p className="danger-text">{provider.last_error}</p>}
              <div className="row-actions">
                {currentUserRole === '系统管理员' && <button onClick={() => onEditProvider(provider)}>编辑</button>}
                <button onClick={() => onCheckProvider(provider)}>检查</button>
                {currentUserRole === '系统管理员' && <button onClick={() => onToggleProvider(provider)}>{provider.is_active ? '禁用' : '启用'}</button>}
              </div>
            </article>
          ))}
        </div>
      </section>

      <section className="panel">
        <div className="section-title">
          <div>
            <h2>客户 AI 与发送节奏</h2>
            <span>平台默认可被客户配置覆盖，任务创建时可覆盖发送节奏</span>
          </div>
          <div className="row-actions">
            <button className="small" disabled={!aiProviders.length} onClick={onEditTenantAi}>编辑 AI 配置</button>
            <button className="small" onClick={onEditScheduling}>编辑发送节奏</button>
          </div>
        </div>
        <div className="summary-grid">
          <article className="summary-card">
            <span>默认模型</span>
            <strong>{aiProviders.find((provider) => provider.id === tenantAiSetting?.default_provider_id)?.provider_name ?? '未配置'}</strong>
            <p><StatusBadge status={tenantAiSetting?.ai_enabled ? '已启用' : '未配置'} label={tenantAiSetting?.ai_enabled ? 'AI 草稿已启用' : 'AI 已关闭'} /></p>
          </article>
          <article className="summary-card">
            <span>失败策略</span>
            <strong>{tenantAiSetting?.fallback_to_mock ? '允许模板回退' : '失败即报错'}</strong>
            <p>温度 {tenantAiSetting?.temperature ?? '-'} / Token {tenantAiSetting?.max_tokens ?? '-'}</p>
          </article>
          <article className="summary-card">
            <span>发送抖动</span>
            <strong>{schedulingSetting?.jitter_min_seconds ?? '-'}-{schedulingSetting?.jitter_max_seconds ?? '-'}s</strong>
            <p>批次 {schedulingSetting?.batch_interval_seconds ?? '-'}s / {schedulingSetting?.respect_send_window ? '遵守时间窗' : '忽略时间窗'}</p>
          </article>
        </div>
      </section>

      <section className="panel">
        <div className="section-title">
          <div>
            <h2>提示词与素材</h2>
            <span>系统决策提示词自动选择业务模板；素材先支持图片和表情包</span>
          </div>
          <div className="row-actions">
            <button className="small" onClick={onCreatePromptTemplate}>新增提示词</button>
            <button className="small" onClick={onCreateMaterial}>新增素材</button>
          </div>
        </div>
        <div className="mini-list">
          {!promptTemplates.length && !materials.length && <p className="muted-line">暂无提示词或素材。可以先新增提示词模板，再创建素材。</p>}
          {promptTemplates.slice(0, 6).map((template) => (
            <article key={template.id}>
              <Badge tone={template.tenant_id ? 'positive' : 'neutral'}>{template.tenant_id ? '客户' : '平台'}</Badge>
              <StatusBadge status={template.is_active ? '已启用' : '禁用'} />
              <strong>{template.name}</strong>
              <span>{template.template_type} / v{template.version}</span>
            </article>
          ))}
          {materials.slice(0, 4).map((material) => (
            <article key={`material-${material.id}`}>
              <Badge tone="warning">{material.material_type}</Badge>
              <StatusBadge status={material.review_status} />
              <strong>{material.title}</strong>
              <span>{material.tags || '无标签'}</span>
            </article>
          ))}
        </div>
      </section>
    </section>
  );
}
