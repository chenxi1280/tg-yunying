import React from 'react';
import { Button, Card, Empty, Input, Segmented, Space, Statistic, Tag, Typography } from 'antd';
import type { TablePaginationConfig } from 'antd/es/table';
import type { BadgeTone } from '../types';
import { formatBeijingDateTime } from '../time';

export function statusTone(status: string | null | undefined): BadgeTone {
  const value = status || '未配置';
  if (['在线', '已发送', '可用', '已授权运营', '已完成', '已同步', '健康', '账号可发言', '可查看', '可发言', '已处理', '已达标', 'redeemed', 'success', 'completed', 'approved', 'target_reached'].includes(value)) return 'positive';
  if (['等待验证码', '等待扫码', '等待2FA', '排队中', '发送中', '同步中', '只读归档', '冷却中', '等待处理', '待确认', '执行中', '待处理', '运行中', '容量不足', '需人工处理', '收尾中', 'unused', 'pending', 'running', 'executing', 'wrapping_up'].includes(value)) return 'warning';
  if (['失败', '受限', '异常', '禁止操作', '已取消', '禁用', '账号不可发言', '不可发言', '内容违规', '账号不可用', '账号受限', '群无权限', '群慢速模式', 'FloodWait', '目标无效', '未知错误', '部分失败', '有失败', '人工停止', '已删除', 'failed', 'rejected', 'stopped', 'deleted'].includes(value)) return 'danger';
  if (['草稿', '未确认', '未配置', '无失败', '未同步', '不可见', '已过期', '已忽略', '待规划', 'disabled', 'draft', 'paused', 'skipped', 'expired'].includes(value)) return 'muted';
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
    <Card className="stat-card" size="small">
      <Space align="start" size={12}>
        <span className="stat-icon">{icon}</span>
        <Statistic title={label} value={value} suffix={<span className="stat-detail">{detail}</span>} />
      </Space>
    </Card>
  );
}

const tagColorByTone: Record<string, string> = {
  positive: 'green',
  warning: 'gold',
  danger: 'red',
  muted: 'default',
  neutral: 'blue',
};

export function Badge({ children, tone }: { children: React.ReactNode; tone: BadgeTone | string }) {
  return <Tag className={`badge ${tone}`} color={tagColorByTone[String(tone)] ?? 'blue'}>{children}</Tag>;
}

export function StatusBadge({ status, label }: { status: string | null | undefined; label?: React.ReactNode }) {
  return <Badge tone={statusTone(status)}>{label ?? status ?? '未配置'}</Badge>;
}

type TableSearchAccessor<T> = keyof T | ((row: T) => unknown);

function valueToSearchText(value: unknown): string {
  if (value == null) return '';
  if (Array.isArray(value)) return value.map(valueToSearchText).join(' ');
  if (value instanceof Date) return formatBeijingDateTime(value);
  if (typeof value === 'object') return Object.values(value as Record<string, unknown>).map(valueToSearchText).join(' ');
  return String(value);
}

export function useAntdTableControls<T>({
  rows,
  search,
  placeholder = '搜索',
  pageSize = 10,
  pageSizeOptions = [10, 20, 50, 100],
}: {
  rows: T[];
  search?: TableSearchAccessor<T>[];
  placeholder?: string;
  pageSize?: number;
  pageSizeOptions?: number[];
}) {
  const [query, setQuery] = React.useState('');
  const [current, setCurrent] = React.useState(1);
  const [currentPageSize, setCurrentPageSize] = React.useState(pageSize);
  const normalizedQuery = query.trim().toLowerCase();

  const filteredRows = React.useMemo(() => {
    if (!normalizedQuery) return rows;
    return rows.filter((row) => {
      const values = search?.length
        ? search.map((accessor) => (typeof accessor === 'function' ? accessor(row) : row[accessor]))
        : Object.values(row as Record<string, unknown>);
      return values.some((value) => valueToSearchText(value).toLowerCase().includes(normalizedQuery));
    });
  }, [normalizedQuery, rows, search]);

  React.useEffect(() => {
    setCurrent(1);
  }, [normalizedQuery, rows]);

  React.useEffect(() => {
    const maxPage = Math.max(1, Math.ceil(filteredRows.length / currentPageSize));
    if (current > maxPage) setCurrent(maxPage);
  }, [current, currentPageSize, filteredRows.length]);

  const pagination: TablePaginationConfig = {
    current,
    pageSize: currentPageSize,
    total: filteredRows.length,
    showSizeChanger: true,
    pageSizeOptions: pageSizeOptions.map(String),
    showTotal: (total, range) => `第 ${range[0]}-${range[1]} 条 / 共 ${total} 条`,
    onChange: (page, nextPageSize) => {
      setCurrent(page);
      setCurrentPageSize(nextPageSize);
    },
  };

  const searchInput = (
    <Input.Search
      allowClear
      className="table-search"
      value={query}
      placeholder={placeholder}
      onChange={(event) => setQuery(event.target.value)}
      onSearch={(value) => setQuery(value.trim())}
      style={{ width: 320, maxWidth: '100%' }}
    />
  );

  return { query, setQuery, filteredRows, pagination, searchInput };
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

export function FormActions({ submitLabel = '保存', onCancel, onSubmit, disabled, loading = false }: { submitLabel?: string; onCancel: () => void; onSubmit: () => void; disabled?: boolean; loading?: boolean }) {
  return (
    <Space className="modal-actions" align="center">
      <Button onClick={onCancel} disabled={loading}>取消</Button>
      <Button type="primary" disabled={disabled} loading={loading} onClick={onSubmit}>{submitLabel}</Button>
    </Space>
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
    <Card className="panel" size="small">
      <div className="section-title">
        <div>
          <h2>{title}</h2>
          {subtitle && <span>{subtitle}</span>}
        </div>
        {children && <div className="row-actions">{children}</div>}
      </div>
    </Card>
  );
}


// ── EmptyState ──

export function EmptyState({ message }: { message: string }) {
  return <Empty className="muted-line" description={message} />;
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
    <Segmented
      className="tabs-row"
      value={activeTab}
      options={tabs}
      onChange={(value) => onTabChange(String(value))}
    />
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
    <Space className="form-field" direction="vertical" size={6}>
      <Typography.Text type="secondary">{label}</Typography.Text>
      {children}
    </Space>
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
  return <Card className={`tg-table ${className ?? ''}`.trim()} size="small">{children}</Card>;
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
    <Card
      size="small"
      className={`table-card ${accent ? statusAccent(accent) : ''}`.trim()}
      onClick={onClick}
    >
      {children}
    </Card>
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
    <Card className={`developer-card ${statusAccent(accent)}`.trim()} size="small">
      {badge}
      <h3>{title}</h3>
      {subtitle && <p className="mini-text">{subtitle}</p>}
      {error && <p className="danger-text">{error || undefined}</p>}
      {children && <div className="row-actions">{children}</div>}
    </Card>
  );
}
