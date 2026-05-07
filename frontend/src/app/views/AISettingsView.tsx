import React from 'react';
import { Button, Card, Descriptions, Empty, List, Space, Typography } from 'antd';
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
      <Card
        className="panel"
        title="AI 供应商"
        extra={currentUserRole === '系统管理员' ? <Button type="primary" onClick={onCreateProvider}>新增供应商</Button> : undefined}
      >
        <Typography.Text type="secondary">MiMo / DeepSeek 使用 OpenAI-Compatible 接口，Key 加密保存</Typography.Text>
        <div className="cards-grid developer-grid">
          {!aiProviders.length && (
            <Empty description="还没有 AI 供应商">
              <Typography.Paragraph type="secondary">请配置真实 OpenAI-Compatible Base URL、模型名和 API Key 后再启用 AI 草稿。</Typography.Paragraph>
              {currentUserRole === '系统管理员' && <Button type="primary" onClick={onCreateProvider}>新增供应商</Button>}
            </Empty>
          )}
          {aiProviders.map((provider) => (
            <Card className={`developer-card ${statusAccent(provider.is_active ? provider.health_status : '禁用')}`} key={provider.id} size="small" title={provider.provider_name}>
              <Space wrap>
                <StatusBadge status={provider.is_active ? provider.health_status : '禁用'} />
                <Badge tone="neutral">{provider.provider_type}</Badge>
              </Space>
              <Typography.Paragraph>{provider.model_name}</Typography.Paragraph>
              <Typography.Paragraph type="secondary" ellipsis>{provider.base_url}</Typography.Paragraph>
              {provider.last_error && <Typography.Paragraph type={provider.health_status === '健康' ? 'warning' : 'danger'}>{provider.last_error}</Typography.Paragraph>}
              <Space wrap>
                {currentUserRole === '系统管理员' && <Button size="small" onClick={() => onEditProvider(provider)}>编辑</Button>}
                <Button size="small" onClick={() => onCheckProvider(provider)}>检查</Button>
                {currentUserRole === '系统管理员' && <Button size="small" danger={provider.is_active} onClick={() => onToggleProvider(provider)}>{provider.is_active ? '禁用' : '启用'}</Button>}
              </Space>
            </Card>
          ))}
        </div>
      </Card>

      <Card
        className="panel"
        title="客户 AI 与发送节奏"
        extra={<Space><Button size="small" disabled={!aiProviders.length} onClick={onEditTenantAi}>编辑 AI 配置</Button><Button size="small" onClick={onEditScheduling}>编辑发送节奏</Button></Space>}
      >
        <Typography.Text type="secondary">平台默认可被客户配置覆盖，任务创建时可覆盖发送节奏</Typography.Text>
        <div className="summary-grid">
          <Card className="summary-card" size="small">
            <span>默认模型</span>
            <strong>{aiProviders.find((provider) => provider.id === tenantAiSetting?.default_provider_id)?.provider_name ?? '未配置'}</strong>
            <p><StatusBadge status={tenantAiSetting?.ai_enabled ? '已启用' : '未配置'} label={tenantAiSetting?.ai_enabled ? 'AI 草稿已启用' : 'AI 已关闭'} /></p>
          </Card>
          <Card className="summary-card" size="small">
            <span>失败策略</span>
            <strong>{tenantAiSetting?.fallback_to_mock ? '允许模板回退' : '失败即报错'}</strong>
            <p>温度 {tenantAiSetting?.temperature ?? '-'} / Token {tenantAiSetting?.max_tokens ?? '-'}</p>
          </Card>
          <Card className="summary-card" size="small">
            <span>发送抖动</span>
            <strong>{schedulingSetting?.jitter_min_seconds ?? '-'}-{schedulingSetting?.jitter_max_seconds ?? '-'}s</strong>
            <p>批次 {schedulingSetting?.batch_interval_seconds ?? '-'}s / {schedulingSetting?.respect_send_window ? '遵守时间窗' : '忽略时间窗'}</p>
          </Card>
        </div>
      </Card>

      <Card
        className="panel"
        title="提示词与素材"
        extra={<Space><Button size="small" onClick={onCreatePromptTemplate}>新增提示词</Button><Button size="small" onClick={onCreateMaterial}>新增素材</Button></Space>}
      >
        <Typography.Text type="secondary">系统决策提示词自动选择业务模板；素材先支持图片和表情包</Typography.Text>
        <List
          className="mini-list"
          dataSource={[
            ...promptTemplates.slice(0, 6).map((template) => ({ kind: 'template' as const, item: template })),
            ...materials.slice(0, 4).map((material) => ({ kind: 'material' as const, item: material })),
          ]}
          locale={{ emptyText: '暂无提示词或素材。可以先新增提示词模板，再创建素材。' }}
          renderItem={(entry) => {
            if (entry.kind === 'template') {
              const template = entry.item;
              return (
                <List.Item>
                  <List.Item.Meta
                    title={<Space><Badge tone={template.tenant_id ? 'positive' : 'neutral'}>{template.tenant_id ? '客户' : '平台'}</Badge><StatusBadge status={template.is_active ? '已启用' : '禁用'} />{template.name}</Space>}
                    description={`${template.template_type} / v${template.version}`}
                  />
                </List.Item>
              );
            }
            const material = entry.item;
            return (
              <List.Item>
                <List.Item.Meta
                  title={<Space><Badge tone="warning">{material.material_type}</Badge><StatusBadge status={material.review_status} />{material.title}</Space>}
                  description={material.tags || '无标签'}
                />
              </List.Item>
            );
          }}
        />
      </Card>
    </section>
  );
}
