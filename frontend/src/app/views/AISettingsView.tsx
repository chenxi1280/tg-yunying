import React from 'react';
import { Alert, App as AntdApp, Button, Card, Descriptions, Empty, Form, Input, List, Select, Space, Typography } from 'antd';
import { api } from '../../shared/api/client';
import type { Account, AiProvider, PromptTemplate, TenantAiSetting, Material, MaterialCacheConfig, MaterialCacheHealth, ContentKeywordRule } from '../types';
import { StatusBadge, Badge } from '../components/shared';
import { statusAccent } from '../utils';

interface Props {
  section?: 'all' | 'providers' | 'resources' | 'slang';
  aiProviders: AiProvider[];
  promptTemplates: PromptTemplate[];
  tenantAiSetting: TenantAiSetting | null;
  accounts?: Account[];
  materials: Material[];
  materialCacheHealth: MaterialCacheHealth | null;
  materialCacheConfig: MaterialCacheConfig | null;
  contentKeywordRules: ContentKeywordRule[];
  currentUserRole: string | undefined;
  canManageAi?: boolean;
  canManagePrompts?: boolean;
  canManageSystem?: boolean;
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
  onSavedMaterialCacheConfig: () => Promise<void>;
  isActionPending: (key: string) => boolean;
  showMaterialAssets?: boolean;
}

type MaterialCacheConfigFormValues = {
  material_cache_input?: string;
  source_media_cache_input?: string;
  material_cache_account_id?: number | null;
};

export default function AISettingsView({
  section = 'all',
  aiProviders,
  promptTemplates,
  tenantAiSetting,
  accounts = [],
  materials,
  materialCacheHealth,
  materialCacheConfig,
  contentKeywordRules,
  currentUserRole,
  canManageAi = false,
  canManagePrompts = false,
  canManageSystem = false,
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
  onSavedMaterialCacheConfig,
  isActionPending,
  showMaterialAssets = true,
}: Props) {
  const { message } = AntdApp.useApp();
  const showProviders = section === 'all' || section === 'providers';
  const showResources = section === 'all' || section === 'resources';
  const showSlang = section === 'all' || section === 'slang';
  const [cacheForm] = Form.useForm<MaterialCacheConfigFormValues>();
  const [savingCacheConfig, setSavingCacheConfig] = React.useState(false);
  const [cacheConfigError, setCacheConfigError] = React.useState('');
  const [cacheConfigRefreshError, setCacheConfigRefreshError] = React.useState('');
  const activeCacheConfigSaveRequestRef = React.useRef({ seq: 0, signature: '' });
  const slangTemplates = promptTemplates.filter((template) => template.template_type.replace(/\s+/g, '') === 'AI黑话词表');
  const businessPromptTemplates = promptTemplates.filter((template) => template.template_type.replace(/\s+/g, '') !== 'AI黑话词表');

  React.useEffect(() => {
    cacheForm.setFieldsValue({
      material_cache_input: materialCacheConfig?.material_cache.raw_input ?? '',
      source_media_cache_input: materialCacheConfig?.source_media_cache.raw_input ?? '',
      material_cache_account_id: materialCacheConfig?.cache_account?.id ?? undefined,
    });
  }, [cacheForm, materialCacheConfig]);

  const cacheAccountOptions = React.useMemo(
    () => accounts
      .filter((account) => !account.deleted_at)
      .map((account) => {
        const username = account.username ? `@${account.username}` : '@-';
        const label = `${account.display_name} / ${account.phone_number || account.phone_masked || '-'} / ${username} / ${account.status} / 健康分 ${Math.round(account.health_score ?? 0)}`;
        return { value: account.id, label };
      }),
    [accounts],
  );

  function materialCacheConfigPayloadSignature(values: MaterialCacheConfigFormValues) {
    return JSON.stringify({
      material_cache_input: values.material_cache_input ?? '',
      source_media_cache_input: values.source_media_cache_input ?? '',
      material_cache_account_id: values.material_cache_account_id ?? null,
    });
  }

  function beginCacheConfigSaveRequest(signature: string) {
    activeCacheConfigSaveRequestRef.current = { seq: activeCacheConfigSaveRequestRef.current.seq + 1, signature };
    return activeCacheConfigSaveRequestRef.current;
  }

  function currentMaterialCacheConfigPayloadSignature() {
    return materialCacheConfigPayloadSignature(cacheForm.getFieldsValue(true));
  }

  function isActiveCacheConfigSaveRequest(request: { seq: number; signature: string }) {
    return activeCacheConfigSaveRequestRef.current.seq === request.seq;
  }

  function isCurrentCacheConfigSaveRequest(request: { seq: number; signature: string }) {
    return isActiveCacheConfigSaveRequest(request) && currentMaterialCacheConfigPayloadSignature() === request.signature;
  }

  async function saveMaterialCacheConfig(values: MaterialCacheConfigFormValues) {
    const saveRequest = beginCacheConfigSaveRequest(materialCacheConfigPayloadSignature(values));
    setSavingCacheConfig(true);
    setCacheConfigError('');
    setCacheConfigRefreshError('');
    try {
      let saved: MaterialCacheConfig;
      try {
        saved = await api<MaterialCacheConfig>('/materials/cache/config', {
          method: 'PATCH',
          body: JSON.stringify({
            ...values,
            material_cache_account_id: values.material_cache_account_id ?? null,
          }),
        });
      } catch (error) {
        if (!isCurrentCacheConfigSaveRequest(saveRequest)) return;
        setCacheConfigError(error instanceof Error ? error.message : '保存缓存配置失败');
        return;
      }
      if (!isCurrentCacheConfigSaveRequest(saveRequest)) return;
      try {
        await onSavedMaterialCacheConfig();
      } catch (error) {
        if (!isCurrentCacheConfigSaveRequest(saveRequest)) return;
        setCacheConfigRefreshError(error instanceof Error ? error.message : String(error));
      }
      if (!isCurrentCacheConfigSaveRequest(saveRequest)) return;
      const warning = saved.material_cache.last_error || saved.source_media_cache.last_error;
      if (warning) {
        void message.warning(warning);
      } else {
        void message.success('缓存配置已保存');
      }
    } finally {
      if (isActiveCacheConfigSaveRequest(saveRequest)) setSavingCacheConfig(false);
    }
  }

  return (
    <section className="view-grid">
      {showProviders && <Card
        className="panel"
        title="AI 供应商"
        extra={canManageAi ? <Button type="primary" onClick={onCreateProvider}>新增供应商</Button> : undefined}
      >
        <Typography.Text type="secondary">MiMo / DeepSeek 使用 OpenAI-Compatible 接口，Key 加密保存</Typography.Text>
        <div className="cards-grid developer-grid">
          {!aiProviders.length && (
            <Empty description="还没有 AI 供应商">
              <Typography.Paragraph type="secondary">请配置真实 OpenAI-Compatible Base URL、模型名和 API Key 后再启用 AI 内容生成。</Typography.Paragraph>
              {canManageAi && <Button type="primary" onClick={onCreateProvider}>新增供应商</Button>}
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
                {canManageAi && <Button size="small" onClick={() => onEditProvider(provider)}>编辑</Button>}
                {canManageAi && <Button size="small" loading={isActionPending(`ai-provider:${provider.id}:check`)} onClick={() => onCheckProvider(provider)}>检查</Button>}
                {canManageAi && <Button size="small" danger={provider.is_active} loading={isActionPending(`ai-provider:${provider.id}:toggle`)} onClick={() => onToggleProvider(provider)}>{provider.is_active ? '禁用' : '启用'}</Button>}
              </Space>
            </Card>
          ))}
        </div>
      </Card>}

      {showProviders && <Card
        className="panel"
        title="AI 默认模型"
        extra={canManageAi ? <Button size="small" disabled={!aiProviders.length} onClick={onEditTenantAi}>编辑 AI 配置</Button> : undefined}
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
            <strong>{tenantAiSetting?.ai_group_model_fallback_enabled ? 'M3 → M2.5' : '仅默认模型'}</strong>
            <p>温度 {tenantAiSetting?.temperature ?? '-'} / Token {tenantAiSetting?.max_tokens ?? '-'}</p>
            <p>Grok {tenantAiSetting?.ai_group_grok_fallback_enabled ? '启用' : '关闭'} / 签到表情 {tenantAiSetting?.ai_group_static_fallback_enabled ? '启用' : '关闭'}</p>
          </Card>
        </div>
      </Card>}

      {showResources && <Card
        className="panel"
        title={showMaterialAssets ? '提示词与素材' : '提示词与素材运行配置'}
        extra={<Space>{canManagePrompts && <Button size="small" onClick={onCreatePromptTemplate}>新增提示词</Button>}{showMaterialAssets && <Button size="small" onClick={onCreateMaterial}>新增素材</Button>}</Space>}
      >
        <Typography.Text type="secondary">
          {showMaterialAssets
            ? '系统决策提示词自动选择业务模板；素材先支持图片和表情包'
            : '系统设置维护提示词和素材缓存运行状态；表情包、头像包、图片和文件素材请到素材中心上传维护'}
        </Typography.Text>
        {!showMaterialAssets && (
          <Card size="small" title="缓存频道" style={{ marginTop: 12, marginBottom: 12 }}>
            {cacheConfigError && <Alert type="error" showIcon message={cacheConfigError} style={{ marginBottom: 12 }} />}
            {cacheConfigRefreshError && <Alert type="error" showIcon message="缓存配置刷新失败" description={cacheConfigRefreshError} style={{ marginBottom: 12 }} />}
            <Form
              form={cacheForm}
              layout="vertical"
              onFinish={saveMaterialCacheConfig}
            >
              <Form.Item
                label="素材缓存频道"
                name="material_cache_input"
                extra={`运行层：${materialCacheConfig?.material_cache.normalized_peer || '-'} / 来源：${materialCacheConfig?.material_cache.source || 'empty'}`}
              >
                <Input placeholder="缓存频道链接 / @username / t.me/c/..." />
              </Form.Item>
              {materialCacheConfig?.material_cache.last_error && <Alert type="warning" showIcon message={materialCacheConfig.material_cache.last_error} style={{ marginBottom: 12 }} />}
              <Form.Item
                label="缓存执行账号"
                name="material_cache_account_id"
                extra={materialCacheConfig?.cache_account ? `当前：${materialCacheConfig.cache_account.display_name} / ${materialCacheConfig.cache_account.phone_masked} / ${materialCacheConfig.cache_account.username ? `@${materialCacheConfig.cache_account.username}` : '@-'} / ${materialCacheConfig.cache_account.status}` : '不选择时按在线账号健康分自动尝试'}
              >
                <Select
                  allowClear
                  showSearch
                  optionFilterProp="label"
                  placeholder="按手机号 / 备注名 / username 搜索缓存执行账号"
                  options={cacheAccountOptions}
                />
              </Form.Item>
              <Form.Item
                label="源媒体缓存频道"
                name="source_media_cache_input"
                extra={`运行层：${materialCacheConfig?.source_media_cache.normalized_peer || '-'} / 来源：${materialCacheConfig?.source_media_cache.source || 'empty'}`}
              >
                <Input placeholder="缓存频道链接 / @username / t.me/c/..." />
              </Form.Item>
              {materialCacheConfig?.source_media_cache.last_error && <Alert type="warning" showIcon message={materialCacheConfig.source_media_cache.last_error} style={{ marginBottom: 12 }} />}
              <Space wrap>
                <Button type="primary" htmlType="submit" loading={savingCacheConfig} disabled={!canManageSystem}>保存缓存配置</Button>
                <Typography.Text type="secondary">留空时沿用 .env；保存后无需重启。</Typography.Text>
              </Space>
            </Form>
          </Card>
        )}
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
                <List.Item actions={canManagePrompts ? [<Button size="small" onClick={() => onEditPromptTemplate(template)}>编辑</Button>] : []}>
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
        extra={canManagePrompts ? <Button size="small" type="primary" onClick={onCreateSlangTemplate}>新增黑话配置</Button> : undefined}
      >
        <Typography.Text type="secondary">这里维护 AI 活群可选的行业黑话和俗语口径；创建 AI 活群任务时选择一套后，会作为系统默认提示词注入大模型。</Typography.Text>
        <List
          className="mini-list"
          dataSource={slangTemplates}
          locale={{ emptyText: '暂无 AI 黑话配置。可以先新增一套，再到 AI 活跃群任务里选择。' }}
          renderItem={(template) => (
            <List.Item actions={canManagePrompts ? [<Button size="small" onClick={() => onEditPromptTemplate(template)}>编辑</Button>] : []}>
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
