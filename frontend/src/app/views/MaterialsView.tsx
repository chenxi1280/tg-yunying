import React from 'react';
import { Alert, Button, Card, Descriptions, Empty, Image, Input, Modal, Popconfirm, Select, Space, Switch, Table, Tabs, Tag, Typography, message } from 'antd';
import { RefreshCcw, ShieldAlert } from 'lucide-react';
import type { Material, MaterialCacheHealth, MaterialImportResult } from '../types';
import { Badge, StatusBadge } from '../components/shared';
import { formatBeijingDateTime } from '../time';
import { api } from '../../shared/api/client';

interface Props {
  materials: Material[];
  materialImports: MaterialImportResult[];
  materialCacheHealth: MaterialCacheHealth | null;
  canUploadMaterials: boolean;
  canManageMaterials: boolean;
  onCreateMaterial: () => void;
  onEditMaterial: (material: Material) => void;
  onDisableMaterial: (material: Material) => void;
  onRestoreMaterial: (material: Material) => void;
  onOpenImportResult: (result: MaterialImportResult) => void;
  onRefresh: () => void | Promise<void>;
  isActionPending: (key: string) => boolean;
}

type MaterialReferenceItem = {
  source_type: string;
  source_id: string;
  title: string;
  status: string;
};

type MaterialReferences = {
  material_id: number;
  summary: Material['reference_summary'];
  items: MaterialReferenceItem[];
};

type MaterialVersionHistory = {
  material_id: number;
  asset_versions: Array<Record<string, any>>;
  tg_ref_versions: Array<Record<string, any>>;
};

type MaterialGroup = {
  id: number;
  tenant_id: number;
  name: string;
  group_type: string;
  description: string;
  is_active: boolean;
  material_count: number;
};

type MaterialGroupForm = {
  name: string;
  group_type: string;
  description: string;
  is_active: boolean;
};

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
  materialImports,
  materialCacheHealth,
  canUploadMaterials,
  canManageMaterials,
  onCreateMaterial,
  onEditMaterial,
  onDisableMaterial,
  onRestoreMaterial,
  onOpenImportResult,
  onRefresh,
  isActionPending,
}: Props) {
  const [detailOpen, setDetailOpen] = React.useState(false);
  const [detailLoading, setDetailLoading] = React.useState(false);
  const [detailMaterial, setDetailMaterial] = React.useState<Material | null>(null);
  const [materialReferences, setMaterialReferences] = React.useState<MaterialReferences | null>(null);
  const [materialVersions, setMaterialVersions] = React.useState<MaterialVersionHistory | null>(null);
  const [cacheBusyId, setCacheBusyId] = React.useState<number | null>(null);
  const [groupOpen, setGroupOpen] = React.useState(false);
  const [groupLoading, setGroupLoading] = React.useState(false);
  const [groupSaving, setGroupSaving] = React.useState(false);
  const [materialGroups, setMaterialGroups] = React.useState<MaterialGroup[]>([]);
  const [editingGroup, setEditingGroup] = React.useState<MaterialGroup | null>(null);
  const [groupForm, setGroupForm] = React.useState<MaterialGroupForm>(emptyMaterialGroupForm());
  const activeMaterialDetailId = React.useRef<number | null>(null);
  const materialDetailRequestSeq = React.useRef(0);
  const materialGroupRequestSeq = React.useRef(0);
  const activeMaterialGroupActionKey = React.useRef('');
  const activeMaterialGroupSaveRequestRef = React.useRef({ seq: 0, signature: '' });
  const counts = cacheCounts(materialCacheHealth);
  const stickerMaterials = React.useMemo(() => materials.filter((item) => materialType(item) === 'sticker'), [materials]);
  const avatarMaterials = React.useMemo(() => materials.filter((item) => materialType(item) === 'avatar'), [materials]);
  const mediaMaterials = React.useMemo(() => materials.filter((item) => materialType(item) === 'media'), [materials]);

  function isActiveMaterialDetail(materialId: number, requestSeq: number) {
    return activeMaterialDetailId.current === materialId && materialDetailRequestSeq.current === requestSeq;
  }

  async function openMaterialDetail(material: Material) {
    const requestSeq = materialDetailRequestSeq.current + 1;
    materialDetailRequestSeq.current = requestSeq;
    activeMaterialDetailId.current = material.id;
    setDetailOpen(true);
    setDetailLoading(true);
    setDetailMaterial(material);
    setMaterialReferences(null);
    setMaterialVersions(null);
    try {
      const [detailResult, referencesResult, versionsResult] = await Promise.allSettled([
        api<Material>(`/materials/${material.id}`),
        api<MaterialReferences>(`/materials/${material.id}/references`),
        api<MaterialVersionHistory>(`/materials/${material.id}/versions`),
      ]);
      if (!isActiveMaterialDetail(material.id, requestSeq)) return;
      if (detailResult.status === 'fulfilled') setDetailMaterial(detailResult.value);
      if (referencesResult.status === 'fulfilled') setMaterialReferences(referencesResult.value);
      if (versionsResult.status === 'fulfilled') setMaterialVersions(versionsResult.value);
      const errors = [
        detailResult.status === 'rejected' ? `基础信息：${detailResult.reason instanceof Error ? detailResult.reason.message : String(detailResult.reason)}` : '',
        referencesResult.status === 'rejected' ? `引用记录：${referencesResult.reason instanceof Error ? referencesResult.reason.message : String(referencesResult.reason)}` : '',
        versionsResult.status === 'rejected' ? `版本记录：${versionsResult.reason instanceof Error ? versionsResult.reason.message : String(versionsResult.reason)}` : '',
      ].filter(Boolean);
      if (errors.length) void message.error(`读取素材详情失败：${errors.join('；')}`);
    } catch (error) {
      if (!isActiveMaterialDetail(material.id, requestSeq)) return;
      void message.error(error instanceof Error ? error.message : '读取素材详情失败');
    } finally {
      if (isActiveMaterialDetail(material.id, requestSeq)) setDetailLoading(false);
    }
  }

  function closeMaterialDetail() {
    activeMaterialDetailId.current = null;
    materialDetailRequestSeq.current += 1;
    setDetailOpen(false);
    setDetailLoading(false);
    setDetailMaterial(null);
    setMaterialReferences(null);
    setMaterialVersions(null);
  }

  async function refreshMaterialCache(material: Material) {
    if (!canManageMaterials) {
      void message.warning('当前账号没有素材管理权限');
      return;
    }
    const detailRequestSeq = materialDetailRequestSeq.current;
    const shouldRefreshDetail = activeMaterialDetailId.current === material.id;
    setCacheBusyId(material.id);
    try {
      const updated = await api<Material>(`/materials/${material.id}/refresh-cache`, {
        method: 'POST',
        body: JSON.stringify({ reason: '素材中心手动刷新缓存' }),
      });
      if (shouldRefreshDetail && materialDetailRequestSeq.current !== detailRequestSeq) return;
      if (shouldRefreshDetail) {
        setDetailMaterial((current) => current?.id === updated.id ? updated : current);
        await openMaterialDetail(updated);
      }
      try {
        await onRefresh();
      } catch (refreshError) {
        void message.error(`刷新素材列表失败：${refreshError instanceof Error ? refreshError.message : String(refreshError)}`);
      }
      void message.success('已提交素材缓存刷新');
    } catch (error) {
      if (shouldRefreshDetail && materialDetailRequestSeq.current !== detailRequestSeq) return;
      void message.error(error instanceof Error ? error.message : '刷新素材缓存失败');
    } finally {
      setCacheBusyId((current) => current === material.id ? null : current);
    }
  }

  function openMaterialGroups() {
    setGroupOpen(true);
    setEditingGroup(null);
    setGroupForm(emptyMaterialGroupForm());
    void loadMaterialGroups();
  }

  async function loadMaterialGroups() {
    const requestSeq = beginMaterialGroupRequest();
    setGroupLoading(true);
    try {
      await fetchMaterialGroups(requestSeq);
    } catch (error) {
      if (!isActiveMaterialGroupRequest(requestSeq)) return;
      void message.error(error instanceof Error ? error.message : '读取素材组失败');
    } finally {
      if (isActiveMaterialGroupRequest(requestSeq)) setGroupLoading(false);
    }
  }

  function beginMaterialGroupRequest() {
    materialGroupRequestSeq.current += 1;
    return materialGroupRequestSeq.current;
  }

  function isActiveMaterialGroupRequest(requestSeq: number) {
    return materialGroupRequestSeq.current === requestSeq;
  }

  function beginMaterialGroupAction(actionKey: string) {
    activeMaterialGroupActionKey.current = actionKey;
    return activeMaterialGroupActionKey.current;
  }

  function isActiveMaterialGroupAction(actionKey: string) {
    return activeMaterialGroupActionKey.current === actionKey;
  }

  function materialGroupSavePayloadSignature(groupId: number | null, payload: MaterialGroupForm) {
    return JSON.stringify({
      id: groupId,
      name: payload.name.trim(),
      group_type: payload.group_type,
      description: payload.description.trim(),
      is_active: payload.is_active,
    });
  }

  function beginMaterialGroupSaveRequest(signature: string) {
    activeMaterialGroupSaveRequestRef.current = { seq: activeMaterialGroupSaveRequestRef.current.seq + 1, signature };
    return activeMaterialGroupSaveRequestRef.current;
  }

  function currentMaterialGroupSavePayloadSignature() {
    return materialGroupSavePayloadSignature(editingGroup?.id ?? null, groupForm);
  }

  function isCurrentMaterialGroupSaveRequest(request: { seq: number; signature: string }) {
    return activeMaterialGroupSaveRequestRef.current.seq === request.seq && currentMaterialGroupSavePayloadSignature() === request.signature;
  }

  async function fetchMaterialGroups(requestSeq: number) {
    const rows = await api<MaterialGroup[]>('/material-groups');
    if (!isActiveMaterialGroupRequest(requestSeq)) return false;
    setMaterialGroups(rows);
    return true;
  }

  async function refreshMaterialGroupsAfterAction(actionLabel: string) {
    const requestSeq = beginMaterialGroupRequest();
    try {
      await fetchMaterialGroups(requestSeq);
    } catch (error) {
      if (!isActiveMaterialGroupRequest(requestSeq)) return;
      const reason = error instanceof Error ? error.message : String(error);
      void message.error(`素材中心数据刷新失败：${actionLabel}操作已完成，但刷新素材组列表失败：${reason}`);
    }
  }

  function editMaterialGroup(group: MaterialGroup) {
    setEditingGroup(group);
    setGroupForm({
      name: group.name,
      group_type: group.group_type,
      description: group.description,
      is_active: group.is_active,
    });
  }

  async function saveMaterialGroup() {
    if (!canManageMaterials) {
      void message.warning('当前账号没有素材管理权限');
      return;
    }
    if (!groupForm.name.trim()) {
      void message.warning('素材组名称不能为空');
      return;
    }
    const actionKey = beginMaterialGroupAction(editingGroup ? `group-save:${editingGroup.id}` : 'group-create');
    const saveRequest = beginMaterialGroupSaveRequest(currentMaterialGroupSavePayloadSignature());
    setGroupSaving(true);
    try {
      const payload = {
        name: groupForm.name.trim(),
        group_type: groupForm.group_type,
        description: groupForm.description.trim(),
        is_active: groupForm.is_active,
      };
      if (editingGroup) {
        await api<MaterialGroup>(`/material-groups/${editingGroup.id}`, { method: 'PATCH', body: JSON.stringify(payload) });
      } else {
        await api<MaterialGroup>('/material-groups', { method: 'POST', body: JSON.stringify(payload) });
      }
      if (!isActiveMaterialGroupAction(actionKey)) return;
      if (!isCurrentMaterialGroupSaveRequest(saveRequest)) return;
      setEditingGroup(null);
      setGroupForm(emptyMaterialGroupForm());
      void message.success('素材组已保存');
      await refreshMaterialGroupsAfterAction('素材组保存');
    } catch (error) {
      if (!isActiveMaterialGroupAction(actionKey)) return;
      if (!isCurrentMaterialGroupSaveRequest(saveRequest)) return;
      void message.error(error instanceof Error ? error.message : '保存素材组失败');
    } finally {
      if (isActiveMaterialGroupAction(actionKey)) setGroupSaving(false);
    }
  }

  async function toggleMaterialGroup(group: MaterialGroup) {
    if (!canManageMaterials) {
      void message.warning('当前账号没有素材管理权限');
      return;
    }
    const actionKey = beginMaterialGroupAction(`group-toggle:${group.id}`);
    setGroupSaving(true);
    try {
      await api<MaterialGroup>(`/material-groups/${group.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ is_active: !group.is_active }),
      });
      if (!isActiveMaterialGroupAction(actionKey)) return;
      await refreshMaterialGroupsAfterAction('素材组启停');
    } catch (error) {
      if (!isActiveMaterialGroupAction(actionKey)) return;
      void message.error(error instanceof Error ? error.message : '更新素材组失败');
    } finally {
      if (isActiveMaterialGroupAction(actionKey)) setGroupSaving(false);
    }
  }

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
      width: 300,
      render: (_: unknown, material: Material) => (
        <Space size={6} wrap>
          <Button size="small" onClick={() => void openMaterialDetail(material)}>详情</Button>
          <Button
            size="small"
            icon={<RefreshCcw size={14} />}
            disabled={!canManageMaterials}
            loading={cacheBusyId === material.id}
            onClick={() => void refreshMaterialCache(material)}
          >
            刷新缓存
          </Button>
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
            <Button onClick={openMaterialGroups}>素材组管理</Button>
            <Button type="primary" disabled={!canUploadMaterials} onClick={onCreateMaterial}>上传素材 / ZIP</Button>
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

      <Card className="panel" title="最近导入结果">
        <Table
          rowKey="import_id"
          size="small"
          dataSource={materialImports}
          pagination={{ pageSize: 5, hideOnSinglePage: true }}
          locale={{ emptyText: <Empty description="暂无 ZIP 导入结果" /> }}
          columns={[
            { title: '压缩包', dataIndex: 'source_filename' },
            { title: '素材包', dataIndex: 'target_group_name' },
            { title: '状态', dataIndex: 'status', width: 110, render: (value: string) => <StatusBadge status={value} /> },
            { title: '成功', dataIndex: 'success_count', width: 80 },
            { title: '跳过', dataIndex: 'skipped_count', width: 80 },
            { title: '失败', dataIndex: 'failed_count', width: 80 },
            {
              title: '失败原因',
              render: (_, row) => row.items.filter((item) => item.status !== 'created').slice(0, 3).map((item) => `${item.file_name}: ${item.reason}`).join('；') || '-',
            },
            { title: '操作', width: 100, render: (_, row) => <Button size="small" onClick={() => onOpenImportResult(row)}>查看详情</Button> },
          ]}
        />
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

      <Modal
        className="tg-modal large"
        title="素材组管理"
        open={groupOpen}
        width={900}
        footer={null}
        onCancel={() => setGroupOpen(false)}
        destroyOnHidden
        centered
      >
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <Card size="small" title={editingGroup ? `编辑素材组：${editingGroup.name}` : '新增素材组'}>
            <div className="policy-grid">
              <label>名称<Input value={groupForm.name} onChange={(event) => setGroupForm((form) => ({ ...form, name: event.target.value }))} /></label>
              <label>类型<Select value={groupForm.group_type} onChange={(value) => setGroupForm((form) => ({ ...form, group_type: value }))} options={materialGroupTypeOptions()} /></label>
              <label>描述<Input value={groupForm.description} onChange={(event) => setGroupForm((form) => ({ ...form, description: event.target.value }))} /></label>
              <label>启用<Switch checked={groupForm.is_active} onChange={(checked) => setGroupForm((form) => ({ ...form, is_active: checked }))} /></label>
            </div>
            <Space style={{ marginTop: 12 }}>
              <Button type="primary" loading={groupSaving} disabled={!canManageMaterials} onClick={() => void saveMaterialGroup()}>{editingGroup ? '保存素材组' : '新增素材组'}</Button>
              {editingGroup && <Button onClick={() => { setEditingGroup(null); setGroupForm(emptyMaterialGroupForm()); }}>取消编辑</Button>}
            </Space>
          </Card>
          <Table<MaterialGroup>
            rowKey="id"
            size="small"
            loading={groupLoading}
            dataSource={materialGroups}
            pagination={{ pageSize: 8, hideOnSinglePage: true }}
            locale={{ emptyText: <Empty description="暂无素材组" /> }}
            columns={[
              { title: '名称', dataIndex: 'name' },
              { title: '类型', dataIndex: 'group_type', width: 140, render: (value) => value || '-' },
              { title: '素材数', dataIndex: 'material_count', width: 90 },
              { title: '描述', dataIndex: 'description', render: (value) => value || '-' },
              { title: '状态', dataIndex: 'is_active', width: 90, render: (value) => <StatusBadge status={value ? 'active' : 'disabled'} label={value ? '启用' : '停用'} /> },
              {
                title: '操作',
                width: 150,
                render: (_, group) => (
                  <Space size={6}>
                    <Button size="small" disabled={!canManageMaterials} onClick={() => editMaterialGroup(group)}>编辑</Button>
                    <Button size="small" disabled={!canManageMaterials} loading={groupSaving} onClick={() => void toggleMaterialGroup(group)}>{group.is_active ? '停用' : '启用'}</Button>
                  </Space>
                ),
              },
            ]}
          />
        </Space>
      </Modal>

      <Modal
        className="tg-modal large"
        title={detailMaterial ? `素材详情：${detailMaterial.title}` : '素材详情'}
        open={detailOpen}
        width={980}
        footer={null}
        onCancel={closeMaterialDetail}
        destroyOnHidden
        centered
      >
        {detailMaterial ? (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Card size="small" title="素材预览" loading={detailLoading}>
              {isPreviewableImage(detailMaterial) ? (
                <Image
                  src={materialPreviewSrc(detailMaterial)}
                  alt={detailMaterial.title}
                  style={{ maxHeight: 260, objectFit: 'contain' }}
                  fallback="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
                />
              ) : (
                <Typography.Paragraph copyable ellipsis={{ rows: 4, expandable: true }}>
                  {detailMaterial.content || detailMaterial.file_name || '暂无可预览内容'}
                </Typography.Paragraph>
              )}
            </Card>

            <Descriptions
              bordered
              size="small"
              column={2}
              items={[
                { key: 'type', label: '类型', children: detailMaterial.material_type || '-' },
                { key: 'status', label: '状态', children: <StatusBadge status={detailMaterial.review_status} /> },
                { key: 'file', label: '文件', children: detailMaterial.file_name || '-' },
                { key: 'size', label: '尺寸/大小', children: `${detailMaterial.width || '-'}x${detailMaterial.height || '-'} / ${formatBytes(detailMaterial.file_size)}` },
                { key: 'cache', label: '缓存状态', children: detailMaterial.cache_ready_status || 'not_cached' },
                { key: 'tg', label: 'TG 引用', children: detailMaterial.tg_cache_message_id ? `${detailMaterial.tg_cache_peer_id || '-'} / ${detailMaterial.tg_cache_message_id}` : '-' },
                { key: 'usage_count', label: '使用次数', children: detailMaterial.usage_count ?? 0 },
                { key: 'last_used', label: '最近使用', children: formatBeijingDateTime(detailMaterial.last_used_at) },
                { key: 'last_cache_error', label: '最近缓存失败', span: 2, children: detailMaterial.last_cache_error || '-' },
                { key: 'caption', label: '说明', span: 2, children: detailMaterial.caption || '-' },
              ]}
            />

            <Card
              size="small"
              title="引用记录"
              extra={<Typography.Text type="secondary">{materialReferences?.summary.total_count ?? detailMaterial.referenced_by_count ?? 0} 条</Typography.Text>}
            >
              <Table<MaterialReferenceItem>
                rowKey={(item) => `${item.source_type}:${item.source_id}`}
                size="small"
                pagination={{ pageSize: 6, hideOnSinglePage: true }}
                dataSource={materialReferences?.items ?? []}
                locale={{ emptyText: <Empty description="暂无引用记录" /> }}
                columns={[
                  { title: '来源', dataIndex: 'source_type', width: 150 },
                  { title: '对象', dataIndex: 'title', render: (value, item) => value || item.source_id },
                  { title: '状态', dataIndex: 'status', width: 120, render: (value) => value || '-' },
                ]}
              />
            </Card>

            <Card size="small" title="版本与缓存记录">
              <Table<Record<string, any>>
                rowKey={(item) => `asset:${item.id}`}
                size="small"
                pagination={false}
                dataSource={materialVersions?.asset_versions ?? []}
                locale={{ emptyText: <Empty description="暂无资产版本" /> }}
                columns={[
                  { title: '资产版本', dataIndex: 'asset_version_id', width: 110, render: (value) => `v${value}` },
                  { title: '文件', dataIndex: 'file_name', render: (value) => value || '-' },
                  { title: '创建人', dataIndex: 'created_by', width: 120 },
                  { title: '时间', dataIndex: 'created_at', width: 180, render: (value) => formatBeijingDateTime(value) },
                ]}
              />
              <Table<Record<string, any>>
                className="sub-table"
                rowKey={(item) => `tg:${item.id}`}
                size="small"
                pagination={false}
                dataSource={materialVersions?.tg_ref_versions ?? []}
                locale={{ emptyText: <Empty description="暂无 TG 缓存版本" /> }}
                columns={[
                  { title: 'TG 版本', dataIndex: 'tg_ref_version_id', width: 110, render: (value) => `v${value}` },
                  { title: '缓存状态', dataIndex: 'cache_status', width: 130 },
                  { title: '消息', key: 'message', render: (_, item) => item.tg_cache_message_id ? `${item.tg_cache_peer_id || '-'} / ${item.tg_cache_message_id}` : '-' },
                  { title: '失败原因', dataIndex: 'failure_reason', render: (value) => value || '-' },
                  { title: '时间', dataIndex: 'created_at', width: 180, render: (value) => formatBeijingDateTime(value) },
                ]}
              />
            </Card>

            <Space>
              <Button
                icon={<RefreshCcw size={14} />}
                disabled={!canManageMaterials}
                loading={cacheBusyId === detailMaterial.id}
                onClick={() => void refreshMaterialCache(detailMaterial)}
              >
                刷新缓存
              </Button>
              <Button onClick={() => onEditMaterial(detailMaterial)} disabled={!canManageMaterials}>编辑素材</Button>
            </Space>
          </Space>
        ) : <Empty description="请选择素材" />}
      </Modal>
    </section>
  );
}

function isPreviewableImage(material: Material) {
  return material.mime_type?.startsWith('image/') || /\.(png|jpe?g|gif|webp|bmp)$/i.test(material.content || material.file_name || '');
}

function materialPreviewSrc(material: Material) {
  const value = material.content || material.file_name || '';
  if (!value || /^https?:\/\//i.test(value) || value.startsWith('/media/')) return value;
  const normalized = value.replace(/\\/g, '/');
  const mediaIndex = normalized.lastIndexOf('/media/');
  if (mediaIndex >= 0) return normalized.slice(mediaIndex);
  for (const marker of ['/material-tmp/', '/source-media-tmp/', '/previews/', '/tmp/']) {
    const index = normalized.lastIndexOf(marker);
    if (index >= 0) {
      const relative = normalized.slice(index + 1);
      return `/media/${relative}`;
    }
  }
  return value;
}

function emptyMaterialGroupForm(): MaterialGroupForm {
  return { name: '', group_type: '', description: '', is_active: true };
}

function materialGroupTypeOptions() {
  return [
    { value: '', label: '通用分组' },
    { value: '图片', label: '图片' },
    { value: '表情包', label: '表情包' },
    { value: '文件', label: '文件' },
    { value: '链接', label: '链接' },
    { value: '组合消息', label: '组合消息' },
  ];
}

function formatBytes(value?: number | null) {
  const size = Number(value || 0);
  if (!size) return '-';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.round(size / 102.4) / 10} KB`;
  return `${Math.round(size / 1024 / 102.4) / 10} MB`;
}
