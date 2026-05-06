import React from 'react';
import { Activity, Bot, CheckCircle2, Database } from 'lucide-react';
import type { UsageLedger, UsageSummary } from '../types';
import { StatCard, StatusBadge } from '../components/shared';

interface Props {
  usageLedgers: UsageLedger[];
  usageSummary: UsageSummary | null;
}

export default function UsageReportsView({ usageLedgers, usageSummary }: Props) {
  return (
    <section className="view-grid">
      <section className="panel">
        <div className="section-title">
          <div>
            <h2>用户用量汇总</h2>
            <span>按用户汇总 token 和费用</span>
          </div>
        </div>
        <div className="stats-grid">
          <StatCard label="总请求" value={usageSummary?.total_requests ?? 0} detail="AI 调用次数" icon={<Bot size={22} />} />
          <StatCard label="总 Token" value={usageSummary?.total_tokens ?? 0} detail="输入输出累计" icon={<Activity size={22} />} />
          <StatCard label="总费用" value={`${usageSummary?.total_cost ?? 0} ${usageSummary?.currency ?? 'CNY'}`} detail="按模型单价结算" icon={<Database size={22} />} />
          <StatCard label="计费请求" value={usageSummary?.billable_requests ?? 0} detail="返回 usage 的真实请求" icon={<CheckCircle2 size={22} />} />
        </div>
        <div className="mini-list">
          {usageSummary?.by_user.map((item) => (
            <article key={item.user_id}>
              <strong>{item.user_name}</strong>
              <span>请求 {item.requests} / Token {item.total_tokens} / 费用 {item.total_cost} {item.currency}</span>
            </article>
          ))}
        </div>
      </section>
      <section className="panel">
        <div className="section-title">
          <div>
            <h2>调用明细</h2>
            <span>记录用户、任务、模型、token 和费用</span>
          </div>
        </div>
        <div className="table">
          {usageLedgers.map((item) => (
            <div className="table-row" key={item.id}>
              <div>
                <strong>{item.provider_name || 'Mock'}</strong>
                <span>{item.model_name} / campaign #{item.campaign_id ?? '-'}</span>
              </div>
              <StatusBadge status={item.request_status === 'success' ? '已完成' : '失败'} />
              <div>
                <strong>Token {item.total_tokens}</strong>
                <span>输入 {item.prompt_tokens} / 输出 {item.completion_tokens}</span>
              </div>
              <div>
                <strong>{item.total_cost} {item.currency}</strong>
                <span>{item.created_at}</span>
              </div>
            </div>
          ))}
        </div>
      </section>
    </section>
  );
}
