import React from 'react';
import { Button, Card, Descriptions, Empty, List, Modal, Typography } from 'antd';
import type { ArchiveItem, ArchiveDetail } from '../types';
import { StatusBadge } from '../components/shared';
import { statusAccent } from '../utils';

interface Props {
  archives: ArchiveItem[];
  archiveDetail: ArchiveDetail | null;
  onOpenArchiveDetail: (archive: ArchiveItem) => void;
  onExportArchive?: (archive: ArchiveItem) => void;
  onRerunArchive?: (archive: ArchiveItem) => void;
  isActionPending: (key: string) => boolean;
}

export default function ArchivesView({ archives, archiveDetail, onOpenArchiveDetail, onExportArchive, onRerunArchive, isActionPending }: Props) {
  const [detailArchiveId, setDetailArchiveId] = React.useState<number | null>(null);
  const detailArchive = archives.find((archive) => archive.id === detailArchiveId) ?? null;
  const currentDetail = archiveDetail?.archive.id === detailArchiveId ? archiveDetail : null;

  function openDetail(archive: ArchiveItem) {
    setDetailArchiveId(archive.id);
    onOpenArchiveDetail(archive);
  }

  return (
    <>
      <Card className="panel" title="群聊归档" extra={<Typography.Text type="secondary">内容、成员清单与新群初始化方案</Typography.Text>}>
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
                <Button type="link" loading={isActionPending(`archive:${archive.id}:detail`)} onClick={() => openDetail(archive)}>查看详情</Button>,
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
        onCancel={() => setDetailArchiveId(null)}
      >
        {!currentDetail ? (
          <Empty description={detailArchive ? '正在读取归档详情' : '暂无归档详情'} />
        ) : (
          <div className="detail-columns">
            <List
              header="消息样例"
              dataSource={currentDetail.messages}
              renderItem={(message) => <List.Item><Typography.Text strong>{message.sender_name}：</Typography.Text>{message.content}</List.Item>}
            />
            <List
              header="成员清单"
              dataSource={currentDetail.members}
              renderItem={(member) => <List.Item><Typography.Text strong>{member.display_name}</Typography.Text> @{member.username ?? '未设置'} / {member.tags} / {member.activity_score}</List.Item>}
            />
            <List
              header="可邀请候选"
              dataSource={currentDetail.invite_candidates}
              renderItem={(member) => <List.Item><Typography.Text strong>{member.display_name}</Typography.Text> @{member.username ?? '未设置'} / {member.tags} / {member.activity_score}</List.Item>}
            />
          </div>
        )}
      </Modal>
    </>
  );
}
