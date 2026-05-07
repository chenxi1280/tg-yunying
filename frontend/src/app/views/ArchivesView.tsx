import React from 'react';
import { Button, Card, Descriptions, Empty, List, Typography } from 'antd';
import type { ArchiveItem, ArchiveDetail } from '../types';
import { StatusBadge } from '../components/shared';
import { statusAccent } from '../utils';

interface Props {
  archives: ArchiveItem[];
  archiveDetail: ArchiveDetail | null;
  onOpenArchiveDetail: (archive: ArchiveItem) => void;
}

export default function ArchivesView({ archives, archiveDetail, onOpenArchiveDetail }: Props) {
  return (
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
            actions={[<Button type="link" onClick={() => onOpenArchiveDetail(archive)}>查看详情</Button>]}
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
      {archiveDetail && (
        <Card className="sub-panel" title={`${archiveDetail.archive.title} 详情`} extra={<Typography.Text type="secondary">消息摘要与成员清单</Typography.Text>}>
          <div className="detail-columns">
            <List
              header="消息样例"
              dataSource={archiveDetail.messages}
              renderItem={(message) => <List.Item><Typography.Text strong>{message.sender_name}：</Typography.Text>{message.content}</List.Item>}
            />
            <List
              header="成员清单"
              dataSource={archiveDetail.members}
              renderItem={(member) => <List.Item><Typography.Text strong>{member.display_name}</Typography.Text> @{member.username ?? '未设置'} / {member.tags} / {member.activity_score}</List.Item>}
            />
          </div>
        </Card>
      )}
    </Card>
  );
}
