import React from 'react';
import { Activity, Send, Smartphone, Users } from 'lucide-react';
import type { Overview, RuntimeConfig } from '../types';
import { StatCard, Badge, StatusBadge } from '../components/shared';
import { riskTone, statusAccent } from '../utils';

interface Props {
  overview: Overview;
  runtime: RuntimeConfig | null;
}

export default function OverviewView({ overview, runtime }: Props) {
  return (
    <section className="view-grid">
      <div className="stats-grid">
        <StatCard label="TG 账号" value={overview.totals.accounts} detail="在线与待登录账号" icon={<Smartphone size={22} />} />
        <StatCard label="可运营群" value={overview.totals.groups} detail="同步群与归档群" icon={<Users size={22} />} />
        <StatCard label="活跃任务" value={overview.totals.campaigns} detail="运营任务总数" icon={<Activity size={22} />} />
        <StatCard label="发送成功率" value={`${overview.rates.send_success}%`} detail="基于已执行消息" icon={<Send size={22} />} />
      </div>
      <section className="panel workflow-panel">
        <div className="section-title">
          <h2>v1 运营闭环</h2>
          <span>账号登录 &gt; 群确认 &gt; AI 草稿 &gt; 审核 &gt; 到期发送 &gt; 归档报表</span>
        </div>
        <div className="flow">
          {['账号接入', '群聊确认', 'AI 生成', '人工审核', '到期发送', '报表审计'].map((item, index) => (
            <div key={item} className="flow-step">
              <strong>{index + 1}</strong>
              <span>{item}</span>
            </div>
          ))}
        </div>
      </section>
      <section className="panel">
        <div className="section-title">
          <h2>风险提醒</h2>
          <span>账号与群维度自动降级</span>
        </div>
        <div className="risk-list">
          {overview.risks.map((risk) => (
            <article key={risk.title}>
              <Badge tone={riskTone(risk.level)}>{risk.level}</Badge>
              <div>
                <strong>{risk.title}</strong>
                <p>{risk.detail}</p>
              </div>
            </article>
          ))}
        </div>
      </section>
    </section>
  );
}
