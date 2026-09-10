import { Alert } from 'antd';
import type { TaskCenterTask } from '../types';

export function SearchTransportNotice({ task }: { task: TaskCenterTask }) {
  const direct = task.type_config?.transport_contract_version === 'sv_current_direct_v1';
  return <Alert showIcon type={direct ? 'info' : 'warning'}
    message={direct ? '当前授权 · 服务器直连' : '历史代理合同 · 待显式迁移'}
    description={direct
      ? '复用当前 SV 授权；代理容量不适用。账号与账号组并发约束仍生效，真实出口证据以执行结果为准。'
      : '历史点击与待确认结果保留。迁移传输方式不会重置目标或自动重放未知操作。'} />;
}
