import React from 'react';
import type { BadgeTone, ConfirmPayload, ResultDialogState } from '../types';

export function statusTone(status: string | null | undefined): BadgeTone {
  const value = status || '未配置';
  if (['在线', '已发送', '已审核', '已授权运营', '已完成', '已同步', '健康', '账号可发言', '可查看', '可发言', '已处理'].includes(value)) return 'positive';
  if (['等待验证码', '等待扫码', '等待2FA', '待审核', '排队中', '发送中', '同步中', '只读归档', '冷却中', '等待处理', '待确认', '执行中', '待处理', '需人工处理'].includes(value)) return 'warning';
  if (['失败', '受限', '异常', '禁止操作', '已驳回', '已取消', '禁用', '账号不可发言', '不可发言', '内容违规', '账号不可用', '账号受限', '群无权限', '群慢速模式', 'FloodWait', '目标无效', '未知错误', '部分失败'].includes(value)) return 'danger';
  if (['草稿', '未确认', '未配置', '无失败', '未同步', '不可见', '已过期', '已忽略'].includes(value)) return 'muted';
  return 'neutral';
}

export function riskTone(level: string | null | undefined): BadgeTone {
  if (level === '高') return 'danger';
  if (level === '中') return 'warning';
  if (level === '低') return 'positive';
  return 'neutral';
}

export function healthTone(score: number): BadgeTone {
  if (score >= 80) return 'positive';
  if (score >= 60) return 'warning';
  return 'danger';
}

export function statusAccent(status: string | null | undefined) {
  return `status-accent ${statusTone(status)}`;
}

export function StatCard({ label, value, detail, icon }: { label: string; value: string | number; detail: string; icon: React.ReactNode }) {
  return (
    <section className="stat-card">
      <div className="stat-icon">{icon}</div>
      <div>
        <p>{label}</p>
        <strong>{value}</strong>
        <span>{detail}</span>
      </div>
    </section>
  );
}

export function Badge({ children, tone }: { children: React.ReactNode; tone: BadgeTone | string }) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

export function StatusBadge({ status, label }: { status: string | null | undefined; label?: React.ReactNode }) {
  return <Badge tone={statusTone(status)}>{label ?? status ?? '未配置'}</Badge>;
}

export function operationLabel(status: string | null | undefined) {
  if (status === '已授权运营') return '可运营';
  if (status === '只读归档') return '仅归档';
  if (status === '禁止操作') return '不可操作';
  if (status === '未确认') return '需确认';
  return status ?? '未配置';
}

export function syncTypeLabel(type: string) {
  if (type === 'groups') return '群聊';
  if (type === 'contacts') return '云联系人';
  if (type === 'codes') return 'TG 官方验证码';
  if (type === 'health') return '健康检查';
  if (type === 'profile_pull') return '资料拉取';
  return type;
}

export function Modal({ title, size = 'medium', children, onClose }: { title: string; size?: 'small' | 'medium' | 'large'; children: React.ReactNode; onClose: () => void }) {
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <section className={`modal ${size}`} role="dialog" aria-modal="true" aria-label={title} onMouseDown={(event) => event.stopPropagation()}>
        <header className="modal-header">
          <h2>{title}</h2>
          <button className="modal-close" onClick={onClose}>关闭</button>
        </header>
        <div className="modal-body">{children}</div>
      </section>
    </div>
  );
}

export function FormActions({ submitLabel = '保存', onCancel, onSubmit, disabled }: { submitLabel?: string; onCancel: () => void; onSubmit: () => void; disabled?: boolean }) {
  return (
    <div className="modal-actions">
      <button onClick={onCancel}>取消</button>
      <button className="primary" disabled={disabled} onClick={onSubmit}>{submitLabel}</button>
    </div>
  );
}

export function ConfirmDialog({ payload, onClose }: { payload: ConfirmPayload; onClose: () => void }) {
  return (
    <Modal title={payload.title} size="small" onClose={onClose}>
      <p className="dialog-message">{payload.message}</p>
      <div className="modal-actions">
        <button onClick={onClose}>取消</button>
        <button className={payload.tone === 'danger' ? 'danger-button' : 'primary'} onClick={() => payload.onConfirm()}>{payload.confirmLabel ?? '确认'}</button>
      </div>
    </Modal>
  );
}

export function ResultDialog({ dialog, onClose }: { dialog: NonNullable<ResultDialogState>; onClose: () => void }) {
  return (
    <Modal title={dialog.title} size="small" onClose={onClose}>
      <p className="dialog-message">{dialog.message}</p>
      <div className="modal-actions">
        <button className="primary" onClick={onClose}>知道了</button>
      </div>
    </Modal>
  );
}


// ── PanelHeader ──

export function PanelHeader({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children?: React.ReactNode;
}) {
  return (
    <section className="panel">
      <div className="section-title">
        <div>
          <h2>{title}</h2>
          {subtitle && <span>{subtitle}</span>}
        </div>
        {children && <div className="row-actions">{children}</div>}
      </div>
    </section>
  );
}


// ── EmptyState ──

export function EmptyState({ message }: { message: string }) {
  return <p className="muted-line">{message}</p>;
}


// ── TabBar ──

export function TabBar({
  tabs,
  activeTab,
  onTabChange,
}: {
  tabs: string[];
  activeTab: string;
  onTabChange: (tab: string) => void;
}) {
  return (
    <div className="tabs-row">
      {tabs.map((tab) => (
        <button
          key={tab}
          className={activeTab === tab ? 'active' : ''}
          onClick={() => onTabChange(tab)}
        >
          {tab}
        </button>
      ))}
    </div>
  );
}


// ── FormField ──

export function FormField({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <label>
      {label}
      {children}
    </label>
  );
}


// ── DataTable ──

export function DataTable({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return <div className={`table ${className ?? ''}`.trim()}>{children}</div>;
}

export function TableRow({
  accent,
  children,
  onClick,
}: {
  accent?: string;
  children: React.ReactNode;
  onClick?: () => void;
}) {
  return (
    <div
      className={`table-row ${accent ? statusAccent(accent) : ''}`.trim()}
      onClick={onClick}
    >
      {children}
    </div>
  );
}


// ── ServiceCard ──

export function ServiceCard({
  accent,
  badge,
  title,
  subtitle,
  error,
  children,
}: {
  accent: string;
  badge: React.ReactNode;
  title: string;
  subtitle?: string;
  error?: string;
  children?: React.ReactNode;
}) {
  return (
    <article className={`developer-card ${statusAccent(accent)}`.trim()}>
      {badge}
      <h3>{title}</h3>
      {subtitle && <p className="mini-text">{subtitle}</p>}
      {error && <p className="danger-text">{error || undefined}</p>}
      {children && <div className="row-actions">{children}</div>}
    </article>
  );
}


// ── Helper ──

export function activityLabel(active: boolean, healthyStatus: string, disabledLabel = '禁用'): string {
  return active ? healthyStatus : disabledLabel;
}
