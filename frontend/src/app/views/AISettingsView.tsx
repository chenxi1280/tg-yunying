import React from 'react';
import { Button, Card, Descriptions, Empty, List, Space, Typography } from 'antd';
import type { AiProvider, PromptTemplate, TenantAiSetting, Material, MaterialCacheHealth, ContentKeywordRule } from '../types';
import { StatusBadge, Badge } from '../components/shared';
import { statusAccent } from '../utils';

interface Props {
  section?: 'all' | 'providers' | 'resources' | 'slang';
  aiProviders: AiProvider[];
  promptTemplates: PromptTemplate[];
  tenantAiSetting: TenantAiSetting | null;
  materials: Material[];
  materialCacheHealth: MaterialCacheHealth | null;
  contentKeywordRules: ContentKeywordRule[];
  currentUserRole: string | undefined;
  onCreateProvider: () => void;
  onEditProvider: (provider: AiProvider) => void;
  onToggleProvider: (provider: AiProvider) => void;
  onCheckProvider: (provider: AiProvider) => void;
  onEditTenantAi: () => void;
  onCreatePromptTemplate: () => void;
  onCreateSlangTemplate: () => void;
  onEditPromptTemplate: (template: PromptTemplate) => void;
  onCreateMaterial: () => void;
  onEditMaterial: (material: Material) => void;
  onCreateKeywordRule: () => void;
  onEditKeywordRule: (rule: ContentKeywordRule) => void;
  isActionPending: (key: string) => boolean;
  showMaterialAssets?: boolean;
}

export default function AISettingsView({
  section = 'all',
  aiProviders,
  promptTemplates,
  tenantAiSetting,
  materials,
  materialCacheHealth,
  contentKeywordRules,
  currentUserRole,
  onCreateProvider,
  onEditProvider,
  onToggleProvider,
  onCheckProvider,
  onEditTenantAi,
  onCreatePromptTemplate,
  onCreateSlangTemplate,
  onEditPromptTemplate,
  onCreateMaterial,
  onEditMaterial,
  onCreateKeywordRule,
  onEditKeywordRule,
  isActionPending,
  showMaterialAssets = true,
}: Props) {
  const showProviders = section === 'all' || section === 'providers';
  const showResources = section === 'all' || section === 'resources';
  const showSlang = section === 'all' || section === 'slang';
  const slangTemplates = promptTemplates.filter((template) => template.template_type.replace(/\s+/g, '') === 'AI黑话词表');
  const businessPromptTemplates = promptTemplates.filter((template) => template.template_type.replace(/\s+/g, '') !== 'AI黑话词表');

  return (
    <section className="view-grid">
      {showProviders && <Card
        className="panel"
        title="AI 供应商"
        extra={currentUserRole === '系统管理员' ? <Button type="primary" onClick={onCreateProvider}>新增供应商</Button> : undefined}
      >
        <Typography.Text type="secondary">MiMo / DeepSeek 使用 OpenAI-Compatible 接口，Key 加密保存</Typography.Text>
        <div className="cards-grid developer-grid">
          {!aiProviders.length && (
            <Empty description="还没有 AI 供应商">
              <Typography.Paragraph type="secondary">请配置真实 OpenAI-Compatible Base URL、模型名和 API Key 后再启用 AI 内容生成。</Typography.Paragraph>
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
                <Button size="small" loading={isActionPending(`ai-provider:${provider.id}:check`)} onClick={() => onCheckProvider(provider)}>检查</Button>
                {currentUserRole === '系统管理员' && <Button size="small" danger={provider.is_active} loading={isActionPending(`ai-provider:${provider.id}:toggle`)} onClick={() => onToggleProvider(provider)}>{provider.is_active ? '禁用' : '启用'}</Button>}
              </Space>
            </Card>
          ))}
        </div>
      </Card>}

      {showProviders && <Card
        className="panel"
        title="AI 默认模型"
        extra={<Button size="small" disabled={!aiProviders.length} onClick={onEditTenantAi}>编辑 AI 配置</Button>}
      >
        <Typography.Text type="secondary">任务创建时可覆盖默认模型；AI 不可用时按这里的失败策略处理</Typography.Text>
        <div className="summary-grid">
          <Card className="summary-card" size="small">
            <span>默认模型</span>
            <strong>{aiProviders.find((provider) => provider.id === tenantAiSetting?.default_provider_id)?.provider_name ?? '未配置'}</strong>
            <p><StatusBadge status={tenantAiSetting?.ai_enabled ? '已启用' : '未配置'} label={tenantAiSetting?.ai_enabled ? 'AI 生成已启用' : 'AI 已关闭'} /></p>
          </Card>
          <Card className="summary-card" size="small">
            <span>失败策略</span>
            <strong>{tenantAiSetting?.fallback_to_mock ? '允许模板回退' : '失败即报错'}</strong>
            <p>温度 {tenantAiSetting?.temperature ?? '-'} / Token {tenantAiSetting?.max_tokens ?? '-'}</p>
          </Card>
        </div>
      </Card>}

      {showResources && <Card
        className="panel"
        title={showMaterialAssets ? '提示词与素材' : '提示词与素材运行配置'}
        extra={<Space><Button size="small" onClick={onCreatePromptTemplate}>新增提示词</Button>{showMaterialAssets && <Button size="small" onClick={onCreateMaterial}>新增素材</Button>}</Space>}
      >
        <Typography.Text type="secondary">
          {showMaterialAssets
            ? '系统决策提示词自动选择业务模板；素材先支持图片和表情包'
            : '系统设置维护提示词和素材缓存运行状态；表情包、头像包、图片和文件素材请到素材中心上传维护'}
        </Typography.Text>
        {materialCacheHealth && (
          <>
            <div className="summary-grid">
              <Card className="summary-card" size="small">
                <span>TG 缓存</span>
                <strong>{materialCacheHealth.material_cache_peer_configured ? '已配置' : '未配置'}</strong>
                <p>源媒体缓存 {materialCacheHealth.source_media_cache_peer_configured ? '已配置' : '未配置'} / 可用账号 {materialCacheHealth.active_cache_account_count}</p>
              </Card>
              <Card className="summary-card" size="small">
                <span>队列状态</span>
                <strong>{materialCacheHealth.waiting_action_count}</strong>
                <p>FloodWait {materialCacheHealth.flood_wait_count} / 失败 {materialCacheHealth.cache_failed_count}</p>
              </Card>
              <Card className="summary-card" size="small">
                <span>最早待缓存</span>
                <strong>{materialCacheHealth.material_oldest_pending_at || '-'}</strong>
                <p>源媒体 {materialCacheHealth.source_media_oldest_pending_at || '-'}</p>
              </Card>
            </div>
            <Space wrap size={[6, 6]} style={{ marginBottom: 12 }}>
              {materialCacheHealth.material_status_counts.map((item) => <StatusBadge key={`material-${item.status}`} status={item.status} label={`素材 ${item.status} ${item.count}`} />)}
              {materialCacheHealth.source_media_status_counts.map((item) => <StatusBadge key={`source-${item.status}`} status={item.status} label={`源媒体 ${item.status} ${item.count}`} />)}
            </Space>
            {materialCacheHealth.recent_errors.length > 0 && (
              <List
                className="mini-list"
                size="small"
                dataSource={materialCacheHealth.recent_errors.slice(0, 5)}
                renderItem={(item) => (
                  <List.Item>
                    <Space orientation="vertical" size={0}>
                      <Typography.Text>{item.scope === 'source_media' ? '源媒体' : '素材'} {item.title}</Typography.Text>
                      <Typography.Text type="secondary">{item.status} / {item.reason || '无失败原因'}</Typography.Text>
                    </Space>
                  </List.Item>
                )}
              />
            )}
          </>
        )}
        <List
          className="mini-list"
          dataSource={[
            ...businessPromptTemplates.slice(0, 6).map((template) => ({ kind: 'template' as const, item: template })),
            ...(showMaterialAssets ? materials.slice(0, 4).map((material) => ({ kind: 'material' as const, item: material })) : []),
          ]}
          locale={{ emptyText: showMaterialAssets ? '暂无提示词或素材。可以先新增提示词模板，再创建素材。' : '暂无提示词模板。素材日常维护请进入素材中心。' }}
          renderItem={(entry) => {
            if (entry.kind === 'template') {
              const template = entry.item;
              return (
                <List.Item actions={[<Button size="small" onClick={() => onEditPromptTemplate(template)}>编辑</Button>]}>
                  <List.Item.Meta
                    title={<Space><Badge tone={template.tenant_id ? 'positive' : 'neutral'}>{template.tenant_id ? '运营空间' : '平台'}</Badge><StatusBadge status={template.is_active ? '已启用' : '禁用'} />{template.name}</Space>}
                    description={`${template.template_type} / v${template.version}`}
                  />
                </List.Item>
              );
            }
            const material = entry.item;
            return (
              <List.Item actions={[<Button size="small" onClick={() => onEditMaterial(material)}>编辑</Button>]}>
                <List.Item.Meta
                  title={<Space><Badge tone="warning">{material.material_type}</Badge><StatusBadge status={material.review_status} label={material.review_status === '已审核' ? '可用' : material.review_status} />{material.title}</Space>}
                  description={material.tags || '无标签'}
                />
              </List.Item>
            );
          }}
        />
      </Card>}

      {showSlang && <Card
        className="panel"
        title="AI 黑话配置"
        extra={<Button size="small" type="primary" onClick={onCreateSlangTemplate}>新增黑话配置</Button>}
      >
        <Typography.Text type="secondary">这里维护 AI 活群可选的行业黑话和俗语口径；创建 AI 活群任务时选择一套后，会作为系统默认提示词注入大模型。</Typography.Text>
        <List
          className="mini-list"
          dataSource={slangTemplates}
          locale={{ emptyText: '暂无 AI 黑话配置。可以先新增一套，再到 AI 活跃群任务里选择。' }}
          renderItem={(template) => (
            <List.Item actions={[<Button size="small" onClick={() => onEditPromptTemplate(template)}>编辑</Button>]}>
              <List.Item.Meta
                title={<Space><Badge tone={template.tenant_id ? 'positive' : 'neutral'}>{template.tenant_id ? '运营空间' : '平台'}</Badge><StatusBadge status={template.is_active ? '已启用' : '禁用'} />{template.name}</Space>}
                description={`v${template.version} / ${template.content.split('\n').map((line) => line.trim()).filter(Boolean).slice(0, 3).join('；') || '空配置'}`}
              />
            </List.Item>
          )}
        />
      </Card>}

    </section>
  );
}
