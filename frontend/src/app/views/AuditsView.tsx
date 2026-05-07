import React from 'react';
import { Database } from 'lucide-react';
import type { AuditLog } from '../types';
import { statusAccent } from '../utils';

interface Props {
  audits: AuditLog[];
}

export default function AuditsView({ audits }: Props) {
  return (
    <section className="panel">
      <div className="section-title">
        <h2>审计安全</h2>
        <span>登录、验证码、发送、归档、权限变更都留痕</span>
      </div>
      <div className="audit-list">
        {!audits.length && <p className="muted-line">暂无审计记录。配置、登录、卡密和账号池操作会写入这里。</p>}
        {audits.map((log) => (
          <article key={log.id} className={statusAccent(log.action.includes('失败') ? '失败' : log.action.includes('禁用') ? '禁用' : log.action.includes('查看') ? '待审核' : '已完成')}>
            <div className="audit-icon"><Database size={16} /></div>
            <div>
              <strong>{log.action}</strong>
              <span>{log.actor} / {log.target_type} / {new Date(log.created_at).toLocaleString()}</span>
              <p>{log.detail || '已记录操作'}</p>
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}
