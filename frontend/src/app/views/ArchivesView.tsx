import React from 'react';
import { Alert, App as AntdApp, Button, Card, Descriptions, Empty, Input, List, Modal, Select, Space, Typography } from 'antd';
import type { ArchiveItem, ArchiveDetail, OperationTarget } from '../types';
import { StatusBadge } from '../components/shared';
import { statusAccent } from '../utils';
import { api } from '../../shared/api/client';

interface Props {
  archives: ArchiveItem[];
  archiveDetail: ArchiveDetail | null;
  onOpenArchiveDetail: (archive: ArchiveItem) => Promise<boolean>;
  onExportArchive?: (archive: ArchiveItem) => void;
  onRerunArchive?: (archive: ArchiveItem) => void;
  onRefresh?: () => Promise<void>;
  isActionPending: (key: string) => boolean;
}

export default function ArchivesView({ archives, archiveDetail, onOpenArchiveDetail, onExportArchive, onRerunArchive, onRefresh, isActionPending }: Props) {
  const { message } = AntdApp.useApp();
  const [detailArchiveId, setDetailArchiveId] = React.useState<number | null>(null);
  const [targets, setTargets] = React.useState<OperationTarget[]>([]);
  const [createOpen, setCreateOpen] = React.useState(false);
  const [selectedTargetId, setSelectedTargetId] = React.useState<number | undefined>();
  const [archiveTitle, setArchiveTitle] = React.useState('');
  const [creating, setCreating] = React.useState(false);
  const [targetError, setTargetError] = React.useState('');
  const [archiveRefreshError, setArchiveRefreshError] = React.useState('');
  const [createError, setCreateError] = React.useState('');
  const detailRequestSeq = React.useRef(0);
  const detailArchive = archives.find((archive) => archive.id === detailArchiveId) ?? null;
  const currentDetail = archiveDetail?.archive.id === detailArchiveId ? archiveDetail : null;
  const archiveTargets = targets.filter((target) => target.target_type === 'group' && target.can_archive && target.linked_group_id);

  React.useEffect(() => {
    api<OperationTarget[]>('/operation-targets?target_type=group')
      .then(setTargets)
      .catch((error: unknown) => {
        setTargets([]);
        setTargetError(error instanceof Error ? error.message : String(error));
      });
  }, []);

  async function openDetail(archive: ArchiveItem) {
    const requestSeq = detailRequestSeq.current + 1;
    detailRequestSeq.current = requestSeq;
    setDetailArchiveId(archive.id);
    const loaded = await onOpenArchiveDetail(archive);
    if (detailRequestSeq.current !== requestSeq) return;
    if (!loaded) setDetailArchiveId(null);
  }

  async function createArchiveFromTarget() {
    const target = targets.find((item) => item.id === selectedTargetId);
    if (!target) {
      void message.error('请选择归档运营目标');
      return;
    }
    setArchiveRefreshError('');
    setCreating(true);
    setCreateError('');
    try {
      await api('/archives', {
        method: 'POST',
        body: JSON.stringify({
          operation_target_id: target.id,
          title: archiveTitle.trim() || `${target.title} 内容与成员归档`,
        }),
      });
    } catch (error) {
      setCreateError(error instanceof Error ? error.message : '创建归档失败');
      return;
    } finally {
      setCreating(false);
    }
    void message.success('归档任务已创建');
    setCreateOpen(false);
    setSelectedTargetId(undefined);
    setArchiveTitle('');
    if (onRefresh) {
      void onRefresh().catch((error: unknown) => {
        setArchiveRefreshError(error instanceof Error ? error.message : String(error));
      });
    }
  }

  return (
    <>
      <Card
        className="panel"
        title="群聊归档"
        extra={(
          <Space>
            <Typography.Text type="secondary">内容、成员清单与新群初始化方案</Typography.Text>
            {onRefresh && <Button type="primary" onClick={() => setCreateOpen(true)}>新建归档</Button>}
          </Space>
        )}
      >
        {targetError && <Alert className="form-alert" type="error" showIcon message="归档目标加载失败" description={targetError} />}
        {archiveRefreshError && <Alert className="form-alert" type="error" showIcon message="归档列表刷新失败" description={archiveRefreshError} />}
        <div className="cards-grid">
          {!archives.length && <Empty description="暂无群聊归档" />}
          {archives.map((archive) => (
            <Card
              className={`archive-card ${statusAccent(archive.status)}`}
              key={archive.id}
              size="small"
              title={archive.title}
              extra={<StatusBadge status={archive.status} />}
              actions={[
                <Button type="link" loading={isActionPending(`archive:${archive.id}:detail`)} onClick={() => void openDetail(archive)}>查看详情</Button>,
                onRerunArchive ? <Button type="link" loading={isActionPending(`archive:${archive.id}:rerun`)} onClick={() => onRerunArchive(archive)}>重跑</Button> : null,
                onExportArchive ? <Button type="link" loading={isActionPending(`archive:${archive.id}:export`)} onClick={() => onExportArchive(archive)}>导出</Button> : null,
              ].filter(Boolean)}
            >
              <Typography.Paragraph>{archive.summary}</Typography.Paragraph>
              <Descriptions size="small" column={2} items={[
                { key: 'messages', label: '消息', children: archive.message_count },
                { key: 'members', label: '成员', children: archive.member_count },
              ]} />
              <div className="plan-box">{archive.new_group_plan}</div>
            </Card>
          ))}
        </div>
      </Card>

      <Modal
        className="tg-modal large"
        title={`${detailArchive?.title ?? '归档'} 详情`}
        open={Boolean(detailArchiveId)}
        width={920}
        footer={null}
        destroyOnHidden
        centered
        onCancel={() => {
          detailRequestSeq.current += 1;
          setDetailArchiveId(null);
        }}
      >
        {!currentDetail ? (
          <Empty description={detailArchive ? '正在读取归档详情' : '暂无归档详情'} />
        ) : (
          <div className="detail-columns">
            <List
              header="消息样例"
              dataSource={currentDetail.messages}
              renderItem={(message) => {
                const phone = message.sender_phone_number || message.sender_phone_masked || '';
                return <List.Item><Typography.Text strong>{message.sender_name}{phone ? ` / ${phone}` : ''}：</Typography.Text>{message.content}</List.Item>;
              }}
            />
            <List
              header="成员清单"
              dataSource={currentDetail.members}
              renderItem={(member) => <List.Item><Typography.Text strong>{member.display_name}</Typography.Text> @{member.username ?? '未设置'} / {member.phone_number || member.phone_masked || '无手机号'} / {member.tags} / {member.activity_score}</List.Item>}
            />
            <List
              header="可邀请候选"
              dataSource={currentDetail.invite_candidates}
              renderItem={(member) => <List.Item><Typography.Text strong>{member.display_name}</Typography.Text> @{member.username ?? '未设置'} / {member.phone_number || member.phone_masked || '无手机号'} / {member.tags} / {member.activity_score}</List.Item>}
            />
          </div>
        )}
      </Modal>

      <Modal
        title="新建归档"
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={createArchiveFromTarget}
        confirmLoading={creating}
        okText="创建"
      >
        <Space direction="vertical" style={{ width: '100%' }}>
          <Typography.Text type="secondary">归档目标来自已确认的运营目标，并会回查关联群资产。</Typography.Text>
          {createError && <Alert type="error" showIcon message="归档创建失败" description={createError} />}
          <Select
            showSearch
            allowClear
            style={{ width: '100%' }}
            placeholder="选择可归档运营目标"
            value={selectedTargetId}
            onChange={(value) => {
              setSelectedTargetId(value);
              const target = targets.find((item) => item.id === value);
              if (target) setArchiveTitle(`${target.title} 内容与成员归档`);
            }}
            options={archiveTargets.map((target) => ({
              value: target.id,
              label: `${target.title} / 可发账号 ${target.available_send_account_count} / 监听账号 ${target.listener_account_count}`,
            }))}
            filterOption={(input, option) => String(option?.label ?? '').toLowerCase().includes(input.toLowerCase())}
          />
          <Input value={archiveTitle} onChange={(event) => setArchiveTitle(event.target.value)} placeholder="归档标题" />
        </Space>
      </Modal>
    </>
  );
}
