import React from 'react';
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
    <section className="panel">
      <div className="section-title">
        <h2>群聊归档</h2>
        <span>内容、成员清单与新群初始化方案</span>
      </div>
      <div className="cards-grid">
        {archives.map((archive) => (
          <article className={`archive-card ${statusAccent(archive.status)}`} key={archive.id}>
            <StatusBadge status={archive.status} />
            <h3>{archive.title}</h3>
            <p>{archive.summary}</p>
            <dl>
              <div><dt>消息</dt><dd>{archive.message_count}</dd></div>
              <div><dt>成员</dt><dd>{archive.member_count}</dd></div>
            </dl>
            <div className="plan-box">{archive.new_group_plan}</div>
            <button className="small" onClick={() => onOpenArchiveDetail(archive)}>查看详情</button>
          </article>
        ))}
      </div>
      {archiveDetail && (
        <div className="sub-panel">
          <div className="section-title">
            <h2>{archiveDetail.archive.title} 详情</h2>
            <span>消息摘要与成员清单</span>
          </div>
          <div className="detail-columns">
            <div>
              <h3>消息样例</h3>
              {archiveDetail.messages.map((message) => (
                <p key={message.id}><strong>{message.sender_name}：</strong>{message.content}</p>
              ))}
            </div>
            <div>
              <h3>成员清单</h3>
              {archiveDetail.members.map((member) => (
                <p key={member.id}><strong>{member.display_name}</strong> @{member.username ?? '未设置'} / {member.tags} / {member.activity_score}</p>
              ))}
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
