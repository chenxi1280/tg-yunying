import React from 'react';
import type { ActivationCode } from '../types';
import { StatusBadge } from '../components/shared';

interface Props {
  activationCodes: ActivationCode[];
  activationBatch: { plan_type: string; quantity: number; note: string };
  setActivationBatch: React.Dispatch<React.SetStateAction<{ plan_type: string; quantity: number; note: string }>>;
  onCreateCodes: () => void;
}

export default function ActivationCodesView({ activationCodes, activationBatch, setActivationBatch, onCreateCodes }: Props) {
  return (
    <section className="panel">
      <div className="section-title">
        <div>
          <h2>卡密管理</h2>
          <span>支持生成月卡和年卡，并查看激活用户、激活时间与生效区间</span>
        </div>
        <div className="row-actions">
          <select value={activationBatch.plan_type} onChange={(event) => setActivationBatch((current) => ({ ...current, plan_type: event.target.value }))}>
            <option value="monthly">月卡</option>
            <option value="yearly">年卡</option>
          </select>
          <input type="number" value={activationBatch.quantity} onChange={(event) => setActivationBatch((current) => ({ ...current, quantity: Number(event.target.value) }))} />
          <input value={activationBatch.note} onChange={(event) => setActivationBatch((current) => ({ ...current, note: event.target.value }))} placeholder="备注" />
          <button className="primary" onClick={onCreateCodes}>批量生成</button>
        </div>
      </div>
      <div className="table">
        {activationCodes.map((item) => (
          <div className="table-row" key={item.id}>
            <div>
              <strong>{item.code}</strong>
              <span>{item.plan_type} / {item.duration_days} 天</span>
            </div>
            <StatusBadge status={item.status} />
            <div>
              <strong>生成：{item.created_by}</strong>
              <span>{item.created_at}</span>
            </div>
            <div>
              <strong>激活时间：{item.redeemed_at ?? '未激活'}</strong>
              <span>{item.subscription_start_at ?? '-'} ~ {item.subscription_end_at ?? '-'}</span>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
