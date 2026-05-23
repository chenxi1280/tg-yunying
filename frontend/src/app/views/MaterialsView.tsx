import React from 'react';
import { Alert, Button, Card, Empty, Popconfirm, Space, Table, Tabs, Tag, Typography } from 'antd';
import { RefreshCcw, ShieldAlert } from 'lucide-react';
import type { Material, MaterialCacheHealth } from '../types';
import { Badge, StatusBadge } from '../components/shared';

interface Props {
  materials: Material[];
  materialCacheHealth: MaterialCacheHealth | null;
  canUploadMaterials: boolean;
  canManageMaterials: boolean;
  onCreateMaterial: () => void;
  onEditMaterial: (material: Material) => void;
  onDisableMaterial: (material: Material) => void;
  onRestoreMaterial: (material: Material) => void;
  onRefresh: () => void;
  isActionPending: (key: string) => boolean;
}

function materialType(material: Material): 'sticker' | 'avatar' | 'media' {
  const text = `${material.material_type} ${material.tags} ${material.title}`.toLowerCase();
  if (/头像|avatar/.test(text)) return 'avatar';
  if (/表情|sticker|emoji|meme/.test(text)) return 'sticker';
  return 'media';
}

function cacheCounts(health: MaterialCacheHealth | null) {
  const counts = new Map((health?.material_status_counts ?? []).map((item) => [item.status, item.count]));
  return {
    ready: counts.get('ready') ?? counts.get('cached') ?? 0,
    pending: counts.get('not_cached') ?? counts.get('pending') ?? 0,
    failed: counts.get('failed') ?? counts.get('cache_failed') ?? health?.cache_failed_count ?? 0,
  };
}

function isDisabled(material: Material) {
  return material.review_status === '已禁用' || material.review_status === '禁用';
}

function referenceText(material: Material) {
  const summary = material.reference_summary;
  if (!summary?.total_count) return '未引用';
  const parts = [
    summary.message_task_count ? `消息 ${summary.message_task_count}` : '',
    summary.action_count ? `动作 ${summary.action_count}` : '',
    summary.rule_version_count ? `规则 ${summary.rule_version_count}` : '',
    summary.operation_plan_count ? `方案 ${summary.operation_plan_count}` : '',
    summary.account_profile_batch_count ? `资料 ${summary.account_profile_batch_count}` : '',
  ].filter(Boolean);
  return parts.join(' / ');
}

export default function MaterialsView({
  materials,
  materialCacheHealth,
  canUploadMaterials,
  canManageMaterials,
  onCreateMaterial,
  onEditMaterial,
  onDisableMaterial,
  onRestoreMaterial,
  onRefresh,
  isActionPending,
}: Props) {
  const counts = cacheCounts(materialCacheHealth);
  const stickerMaterials = React.useMemo(() => materials.filter((item) => materialType(item) === 'sticker'), [materials]);
  const avatarMaterials = React.useMemo(() => materials.filter((item) => materialType(item) === 'avatar'), [materials]);
  const mediaMaterials = React.useMemo(() => materials.filter((item) => materialType(item) === 'media'), [materials]);

  const columns = [
    {
      title: '素材',
      dataIndex: 'title',
      render: (_: string, material: Material) => (
        <Space orientation="vertical" size={0}>
          <Typography.Text strong>{material.title}</Typography.Text>
          <Typography.Text type="secondary">{material.file_name || material.content}</Typography.Text>
        </Space>
      ),
    },
    { title: '类型', dataIndex: 'material_type', width: 120, render: (value: string) => <Tag>{value || '未分类'}</Tag> },
    {
      title: '状态',
      dataIndex: 'review_status',
      width: 120,
      render: (value: string) => (
        <StatusBadge status={value === '已审核' ? 'success' : 'warning'} label={value || '待审核'} />
      ),
    },
    { title: '标签/分组', dataIndex: 'tags', render: (value: string) => value || '-' },
    { title: '缓存', dataIndex: 'cache_ready_status', width: 140, render: (value: string) => <StatusBadge status={value || 'not_cached'} label={value || 'not_cached'} /> },
    {
      title: '版本',
      width: 110,
      render: (_: unknown, material: Material) => `资产 v${material.asset_version_id} / TG v${material.tg_ref_version_id}`,
    },
    {
      title: '引用影响',
      width: 190,
      render: (_: unknown, material: Material) => (
        <Typography.Text type={(material.reference_summary?.total_count ?? material.referenced_by_count ?? 0) ? undefined : 'secondary'}>
          {referenceText(material)}
        </Typography.Text>
      ),
    },
    {
      title: '操作',
      width: 190,
      render: (_: unknown, material: Material) => (
        <Space size={6}>
          <Button size="small" disabled={!canManageMaterials} onClick={() => onEditMaterial(material)}>编辑</Button>
          {isDisabled(material) ? (
            <Button
              size="small"
              icon={<RefreshCcw size={14} />}
              disabled={!canManageMaterials}
              loading={isActionPending(`material:${material.id}:restore`)}
              onClick={() => onRestoreMaterial(material)}
            >
              恢复
            </Button>
          ) : (
            <Popconfirm
              title="禁用素材"
              description={`保留引用关系，仅停止后续使用 ${material.title}`}
              okText="禁用"
              cancelText="取消"
              onConfirm={() => onDisableMaterial(material)}
            >
              <Button
                size="small"
                danger
                icon={<ShieldAlert size={14} />}
                disabled={!canManageMaterials}
                loading={isActionPending(`material:${material.id}:disable`)}
              >
                禁用
              </Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ];

  function renderTable(dataSource: Material[], emptyText: string) {
    return (
      <Table
        rowKey="id"
        size="small"
        dataSource={dataSource}
        columns={columns}
        pagination={{ pageSize: 8, hideOnSinglePage: true }}
        locale={{ emptyText: <Empty description={emptyText} /> }}
      />
    );
  }

  return (
    <section className="view-grid">
      <Card
        className="panel"
        title="素材中心"
        extra={(
          <Space>
            <Button icon={<RefreshCcw size={16} />} loading={isActionPending('app:refresh')} onClick={onRefresh}>刷新</Button>
            <Button type="primary" disabled={!canUploadMaterials} onClick={onCreateMaterial}>上传素材</Button>
          </Space>
        )}
      >
        <Typography.Text type="secondary">
          表情包、头像包、图片、文件和组合消息素材在这里统一维护；系统设置只保留素材缓存账号、上传限制和临时文件 TTL 等运行配置。
        </Typography.Text>
        <div className="summary-grid">
          <Card className="summary-card" size="small">
            <span>素材总数</span>
            <strong>{materials.length}</strong>
            <p>可用 {materials.filter((item) => !isDisabled(item)).length} / 已禁用 {materials.filter(isDisabled).length}</p>
          </Card>
          <Card className="summary-card" size="small">
            <span>TG 缓存</span>
            <strong>{materialCacheHealth?.material_cache_peer_configured ? '已配置' : '未配置'}</strong>
            <p>缓存账号 {materialCacheHealth?.active_cache_account_count ?? 0} / 源媒体 {materialCacheHealth?.source_media_cache_peer_configured ? '已配置' : '未配置'}</p>
          </Card>
          <Card className="summary-card" size="small">
            <span>缓存状态</span>
            <strong>{counts.ready}</strong>
            <p>待缓存 {counts.pending} / 失败 {counts.failed}</p>
          </Card>
        </div>
        {materialCacheHealth?.recent_errors.length ? (
          <Alert
            type="warning"
            showIcon
            title={`最近缓存异常 ${materialCacheHealth.recent_errors.length} 条`}
            description={materialCacheHealth.recent_errors.slice(0, 2).map((item) => `${item.title}: ${item.reason || item.status}`).join('；')}
          />
        ) : null}
      </Card>

      <Card className="panel" title="素材资产">
        <Tabs
          items={[
            { key: 'all', label: `全部 ${materials.length}`, children: renderTable(materials, '暂无素材，可以先上传表情包、头像包或图片文件。') },
            { key: 'stickers', label: `表情包 ${stickerMaterials.length}`, children: renderTable(stickerMaterials, '暂无表情包素材。') },
            { key: 'avatars', label: `头像包 ${avatarMaterials.length}`, children: renderTable(avatarMaterials, '暂无头像包素材。') },
            { key: 'media', label: `图片/文件 ${mediaMaterials.length}`, children: renderTable(mediaMaterials, '暂无图片、文件或组合消息素材。') },
          ]}
        />
      </Card>

      <Card className="panel" title="引用与边界">
        <Space wrap size={[8, 8]}>
          <Badge tone="positive">消息发送引用</Badge>
          <Badge tone="positive">任务中心引用</Badge>
          <Badge tone="positive">规则中心引用</Badge>
          <Badge tone="positive">账号资料初始化引用</Badge>
          <Badge tone="neutral">系统设置仅运行配置</Badge>
        </Space>
        <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
          被任务、规则或发送记录引用的素材不做物理删除；禁用 / 恢复、资产版本、TG 引用版本和引用影响范围均保留在素材中心统一操作。
        </Typography.Paragraph>
      </Card>
    </section>
  );
}
