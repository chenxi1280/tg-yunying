import React from 'react';
import { Archive, MessageSquareText } from 'lucide-react';
import type { Group } from '../types';
import { StatusBadge } from '../components/shared';
import { statusAccent, operationLabel } from '../utils';

interface Props {
  groups: Group[];
  selectedGroup: Group | undefined;
  selectedGroupId: number | null;
  setSelectedGroupId: (id: number | null) => void;
  onCreateCampaign: (groupId?: number) => void;
  onCreateArchive: () => void;
  onAuthorizeGroup: (status: string) => void;
  onEditGroupPolicy: () => void;
  onOpenConfirm: (payload: {
    title: string;
    message: string;
    confirmLabel: string;
    tone?: 'normal' | 'danger';
    onConfirm: () => void | Promise<void>;
  }) => void;
}

export default function GroupsView({
  groups,
  selectedGroup,
  selectedGroupId,
  setSelectedGroupId,
  onCreateCampaign,
  onCreateArchive,
  onAuthorizeGroup,
  onEditGroupPolicy,
  onOpenConfirm,
}: Props) {
  return (
    <section className="split-view">
      <div className="panel">
        <div className="section-title">
          <h2>群聊库</h2>
          <span>按可运营、仅归档和不可操作来管理群聊使用范围</span>
        </div>
        <div className="group-list">
          {groups.map((group) => (
            <button key={group.id} className={`${selectedGroupId === group.id ? 'selected group-card' : 'group-card'} ${statusAccent(group.auth_status)}`} onClick={() => setSelectedGroupId(group.id)}>
              <strong>{group.title}</strong>
              <span>{group.member_count.toLocaleString()} 成员 / {group.group_type}</span>
              <StatusBadge status={group.auth_status} label={operationLabel(group.auth_status)} />
            </button>
          ))}
        </div>
      </div>
      {selectedGroup && (
        <div className="panel detail-panel">
          <div className="section-title">
            <h2>{selectedGroup.title}</h2>
            <span>{selectedGroup.topic_direction}</span>
          </div>
          <dl className="detail-list">
            <div><dt>活跃时间</dt><dd>{selectedGroup.active_window}</dd></div>
            <div><dt>每日上限</dt><dd>{selectedGroup.daily_limit} 条</dd></div>
            <div><dt>审核策略</dt><dd><StatusBadge status={selectedGroup.require_review ? '待审核' : '已审核'} label={selectedGroup.require_review ? '需要人工审核' : '规则内自动'} /></dd></div>
            <div><dt>可发言</dt><dd><StatusBadge status={selectedGroup.can_send ? '可发言' : '不可发言'} /></dd></div>
            <div><dt>监听续聊</dt><dd><StatusBadge status={selectedGroup.listener_enabled ? '已启用' : '未配置'} label={selectedGroup.listener_enabled ? `${selectedGroup.listener_account_ids.length} 个监听号` : '未启用'} /></dd></div>
            <div><dt>自动发送</dt><dd>{selectedGroup.listener_auto_reply_enabled ? '监听触发后自动排队' : '只采集上下文'}</dd></div>
          </dl>
          {selectedGroup.listener_last_error && <p className="danger-text">{selectedGroup.listener_last_error}</p>}
          <div className="detail-actions">
            <button className="primary" onClick={() => onCreateCampaign(selectedGroup.id)}><MessageSquareText size={18} />用此群创建任务</button>
            <button onClick={() => onOpenConfirm({
              title: '创建群归档',
              message: `确认归档「${selectedGroup.title}」的消息与成员清单？`,
              confirmLabel: '创建归档',
              onConfirm: onCreateArchive,
            })}><Archive size={18} />创建归档</button>
            {['已授权运营', '只读归档', '禁止操作'].map((status) => (
              <button key={status} onClick={() => onOpenConfirm({
                title: '更新群使用范围',
                message: `确认将「${selectedGroup.title}」设置为「${operationLabel(status)}」？`,
                confirmLabel: '确认更新',
                tone: status === '禁止操作' ? 'danger' : 'normal',
                onConfirm: () => onAuthorizeGroup(status),
              })}>{operationLabel(status)}</button>
            ))}
          </div>
          <div className="sub-panel">
            <div className="section-title">
              <div>
                <h2>运营配置</h2>
                <span>限频、审核和内容规则</span>
              </div>
              <button className="small" onClick={onEditGroupPolicy}>编辑运营配置</button>
            </div>
            <div className="summary-grid">
              <article className="summary-card"><span>冷却规则</span><strong>账号 {selectedGroup.account_cooldown_seconds}s</strong><p>群 {selectedGroup.group_cooldown_seconds}s</p></article>
              <article className="summary-card"><span>内容边界</span><strong>{selectedGroup.banned_words || '未配置禁用词'}</strong><p>{selectedGroup.link_whitelist || '未配置链接白名单'}</p></article>
              <article className="summary-card"><span>话题方向</span><strong>{selectedGroup.topic_direction || '未设置'}</strong><p>{selectedGroup.require_review ? '需要人工审核' : '规则内自动'}</p></article>
              <article className="summary-card"><span>监听配置</span><strong>{selectedGroup.listener_enabled ? `${selectedGroup.listener_interval_seconds}s 轮询` : '未启用'}</strong><p>上下文 {selectedGroup.listener_context_limit} 条</p></article>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
