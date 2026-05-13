import React from 'react';
import { Activity, CheckCircle2, Database, Send, ShieldAlert, Smartphone, Users } from 'lucide-react';
import { Card, List, Steps, Typography } from 'antd';
import type { Overview, RuntimeConfig } from '../types';
import { StatCard, Badge, StatusBadge } from '../components/shared';
import { riskTone } from '../utils';

interface Props {
  overview: Overview;
  runtime: RuntimeConfig | null;
}

export default function OverviewView({ overview, runtime }: Props) {
  return (
    <section className="view-grid">
      <div className="stats-grid">
        <StatCard label="TG 账号" value={overview.totals.accounts} detail="在线与待登录账号" icon={<Smartphone size={22} />} />
        <StatCard label="运营目标" value={overview.totals.targets ?? overview.totals.groups} detail={`群/频道资产 ${overview.totals.groups}`} icon={<Users size={22} />} />
        <StatCard label="运行中任务" value={overview.queue.running_tasks ?? 0} detail={`总任务 ${overview.totals.tasks ?? 0}`} icon={<Activity size={22} />} />
        <StatCard label="发送成功率" value={`${overview.rates.send_success}%`} detail="基于已执行消息" icon={<Send size={22} />} />
        <StatCard label="待执行项" value={overview.queue.pending_actions ?? overview.queue.queued ?? 0} detail="pending/executing 动作" icon={<Activity size={22} />} />
        <StatCard label="失败任务" value={overview.queue.failed_tasks ?? 0} detail={`失败执行项 ${overview.queue.failed_actions ?? overview.queue.failed ?? 0}`} icon={<ShieldAlert size={22} />} />
        <StatCard label="监听异常" value={overview.queue.listener_errors ?? 0} detail="群/频道监听错误" icon={<Database size={22} />} />
        <StatCard label="启用规则" value={overview.totals.rules ?? 0} detail="规则条件与敏感表达" icon={<CheckCircle2 size={22} />} />
      </div>
      <Card className="panel workflow-panel" title="新版运营闭环" extra={<Typography.Text type="secondary">账号接入 &gt; 目标确认 &gt; 规则配置 &gt; 任务执行 &gt; 数据复盘 &gt; 审计留痕</Typography.Text>}>
        <Steps
          className="flow"
          size="small"
          current={runtime?.can_create_tg_account ? 1 : 0}
          items={['账号接入', '目标确认', '规则配置', '监听/AI/执行', '数据复盘', '审计留痕'].map((title) => ({ title }))}
        />
      </Card>
      <Card className="panel" title="风险提醒" extra={<Typography.Text type="secondary">账号与群维度自动降级</Typography.Text>}>
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
