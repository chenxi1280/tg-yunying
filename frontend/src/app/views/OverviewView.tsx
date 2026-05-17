import React from 'react';
import { Activity, Send, ShieldAlert, Smartphone, Users } from 'lucide-react';
import { Card, Empty, List, Typography } from 'antd';
import type { Overview } from '../types';
import { StatCard, Badge } from '../components/shared';
import { riskTone } from '../utils';

type ActivityPoint = NonNullable<Overview['activity_24h']>[number];
type MetricKey = 'sent_messages' | 'likes' | 'comments' | 'success_rate' | 'failure_rate';

interface Props {
  overview: Overview;
}

const VOLUME_SERIES: Array<{ key: MetricKey; label: string; color: string }> = [
  { key: 'sent_messages', label: '发送', color: '#1677ff' },
  { key: 'likes', label: '点赞', color: '#16a34a' },
  { key: 'comments', label: '评论', color: '#d97706' },
];

const RATE_SERIES: Array<{ key: MetricKey; label: string; color: string }> = [
  { key: 'success_rate', label: '成功率', color: '#16a34a' },
  { key: 'failure_rate', label: '失败率', color: '#dc2626' },
];

export default function OverviewView({ overview }: Props) {
  const activity = React.useMemo(() => normalizedActivity(overview.activity_24h), [overview.activity_24h]);
  const totals = activity.reduce(
    (acc, item) => ({
      sent: acc.sent + item.sent_messages,
      likes: acc.likes + item.likes,
      comments: acc.comments + item.comments,
      success: acc.success + item.success,
      failed: acc.failed + item.failed,
      total: acc.total + item.total,
    }),
    { sent: 0, likes: 0, comments: 0, success: 0, failed: 0, total: 0 },
  );
  const successRate = totals.total ? Math.round((totals.success * 1000) / totals.total) / 10 : 0;
  const failureRate = totals.total ? Math.round((totals.failed * 1000) / totals.total) / 10 : 0;

  return (
    <section className="view-grid">
      <div className="stats-grid">
        <StatCard label="TG 账号" value={overview.totals.accounts} detail="在线与待登录账号" icon={<Smartphone size={22} />} />
        <StatCard label="运营目标" value={overview.totals.targets ?? overview.totals.groups} detail={`群/频道资产 ${overview.totals.groups}`} icon={<Users size={22} />} />
        <StatCard label="运行中任务" value={overview.queue.running_tasks ?? 0} detail={`总任务 ${overview.totals.tasks ?? 0}`} icon={<Activity size={22} />} />
        <StatCard label="24小时发送" value={totals.sent} detail={`点赞 ${totals.likes} / 评论 ${totals.comments}`} icon={<Send size={22} />} />
        <StatCard label="成功率" value={`${successRate}%`} detail={`失败率 ${failureRate}%`} icon={<Activity size={22} />} />
        <StatCard label="待执行项" value={overview.queue.pending_actions ?? overview.queue.queued ?? 0} detail="pending/executing 动作" icon={<Activity size={22} />} />
        <StatCard label="失败任务" value={overview.queue.failed_tasks ?? 0} detail={`失败执行项 ${overview.queue.failed_actions ?? overview.queue.failed ?? 0}`} icon={<ShieldAlert size={22} />} />
        <StatCard label="风险提醒" value={overview.risks.length} detail="账号、目标、监听与执行异常" icon={<ShieldAlert size={22} />} />
      </div>

      <div className="overview-chart-grid">
        <Card className="panel overview-chart-card" title="24小时运营趋势" extra={<Legend items={VOLUME_SERIES} />}>
          <LineChart data={activity} series={VOLUME_SERIES} maxValue={maxOf(activity, ['sent_messages', 'likes', 'comments'])} suffix="次" />
        </Card>
        <Card className="panel overview-chart-card" title="每小时互动拆分" extra={<Legend items={VOLUME_SERIES} />}>
          <HourlyBars data={activity} />
        </Card>
        <Card className="panel overview-chart-card" title="成功率与失败率" extra={<Legend items={RATE_SERIES} />}>
          <LineChart data={activity} series={RATE_SERIES} maxValue={100} suffix="%" />
        </Card>
      </div>

      <Card className="panel" title="风险提醒" extra={<Typography.Text type="secondary">保留账号、目标、监听与执行风险</Typography.Text>}>
        <List
          className="risk-list"
          dataSource={overview.risks}
          locale={{ emptyText: '暂无风险提醒。' }}
          renderItem={(risk) => (
            <List.Item>
              <List.Item.Meta
                avatar={<Badge tone={riskTone(risk.level)}>{risk.level}</Badge>}
                title={risk.title}
                description={risk.detail}
              />
            </List.Item>
          )}
        />
      </Card>
    </section>
  );
}

function normalizedActivity(items?: ActivityPoint[]): ActivityPoint[] {
  if (items?.length) return items;
  const now = new Date();
  return Array.from({ length: 24 }, (_, index) => {
    const hour = new Date(now);
    hour.setHours(now.getHours() - 23 + index, 0, 0, 0);
    return {
      hour: `${String(hour.getHours()).padStart(2, '0')}:00`,
      sent_messages: 0,
      likes: 0,
      comments: 0,
      success: 0,
      failed: 0,
      total: 0,
      success_rate: 0,
      failure_rate: 0,
    };
  });
}

function maxOf(data: ActivityPoint[], keys: MetricKey[]): number {
  const maxValue = Math.max(...data.flatMap((item) => keys.map((key) => Number(item[key] || 0))));
  return Math.max(1, maxValue);
}

function Legend({ items }: { items: Array<{ key: string; label: string; color: string }> }) {
  return (
    <span className="chart-legend">
      {items.map((item) => (
        <span key={item.key}>
          <i style={{ background: item.color }} />
          {item.label}
        </span>
      ))}
    </span>
  );
}

function LineChart({ data, series, maxValue, suffix }: { data: ActivityPoint[]; series: Array<{ key: MetricKey; label: string; color: string }>; maxValue: number; suffix: string }) {
  if (!data.length) return <Empty description="暂无数据" />;
  const width = 720;
  const height = 230;
  const padding = { top: 18, right: 18, bottom: 32, left: 42 };
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const xFor = (index: number) => padding.left + (plotWidth * index) / Math.max(1, data.length - 1);
  const yFor = (value: number) => padding.top + plotHeight - (plotHeight * Math.min(maxValue, Math.max(0, value))) / maxValue;
  const yTicks = [0, Math.round(maxValue / 2), maxValue];

  return (
    <div className="chart-wrap">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="24小时折线图">
        {yTicks.map((tick) => (
          <g key={tick}>
            <line className="chart-grid-line" x1={padding.left} x2={width - padding.right} y1={yFor(tick)} y2={yFor(tick)} />
            <text className="chart-axis-label" x={padding.left - 10} y={yFor(tick) + 4} textAnchor="end">{tick}{suffix}</text>
          </g>
        ))}
        {data.map((item, index) => index % 4 === 0 || index === data.length - 1 ? (
          <text key={item.hour} className="chart-axis-label" x={xFor(index)} y={height - 8} textAnchor="middle">{item.hour}</text>
        ) : null)}
        {series.map((item) => (
          <polyline
            key={item.key}
            fill="none"
            stroke={item.color}
            strokeWidth={3}
            strokeLinecap="round"
            strokeLinejoin="round"
            points={data.map((point, index) => `${xFor(index)},${yFor(Number(point[item.key] || 0))}`).join(' ')}
          />
        ))}
        {series.flatMap((item) => data.map((point, index) => (
          <circle key={`${item.key}-${point.hour}`} cx={xFor(index)} cy={yFor(Number(point[item.key] || 0))} r={index % 4 === 0 || index === data.length - 1 ? 3.5 : 2.2} fill={item.color}>
            <title>{`${point.hour} ${item.label}: ${point[item.key]}${suffix}`}</title>
          </circle>
        )))}
      </svg>
    </div>
  );
}

function HourlyBars({ data }: { data: ActivityPoint[] }) {
  if (!data.length) return <Empty description="暂无数据" />;
  const maxValue = Math.max(1, ...data.map((item) => item.sent_messages + item.likes + item.comments));
  return (
    <div className="hourly-bars" aria-label="每小时互动柱状图">
      {data.map((item, index) => {
        const sentHeight = Math.max(2, Math.round((item.sent_messages / maxValue) * 100));
        const likeHeight = Math.max(2, Math.round((item.likes / maxValue) * 100));
        const commentHeight = Math.max(2, Math.round((item.comments / maxValue) * 100));
        return (
          <div className="hourly-bar" key={item.hour}>
            <div className="hourly-bar-stack" title={`${item.hour} 发送 ${item.sent_messages}，点赞 ${item.likes}，评论 ${item.comments}`}>
              <span className="bar sent" style={{ height: `${sentHeight}%` }} />
              <span className="bar like" style={{ height: `${likeHeight}%` }} />
              <span className="bar comment" style={{ height: `${commentHeight}%` }} />
            </div>
            {(index % 4 === 0 || index === data.length - 1) && <small>{item.hour}</small>}
          </div>
        );
      })}
    </div>
  );
}
