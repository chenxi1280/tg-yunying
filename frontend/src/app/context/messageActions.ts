import { api } from '../../shared/api/client';
import type {
  AccountDetail,
  AccountPoolDetail,
  Contact,
  MessageSendBatchCreate,
  MessageSendTaskCreate,
  MessageTask,
} from '../types';

interface MessageActionParams {
  accountDetail: AccountDetail | null;
  accountPoolDetail: AccountPoolDetail | null;
  directMessageForm: { target_peer_id: string; target_display: string; content: string };
  modalType: string | null;
  poolDirectAccountId: number | '';
  setAccountDetailTab: (tab: string) => void;
  setBusy: (busy: string) => void;
  setDirectMessageForm: (form: { target_peer_id: string; target_display: string; content: string }) => void;
  setPoolDirectAccountId: (id: number | '') => void;
  setTasks: (updater: (current: MessageTask[]) => MessageTask[]) => void;
  refresh: () => Promise<void>;
  refreshAccountDetail: () => Promise<void>;
  refreshAccountPoolDetail: () => Promise<void>;
  showResult: (title: string, detail: string) => void;
}

export function createMessageActions(params: MessageActionParams) {
  function startDirectMessageToContact(contact: Contact) {
    if (params.modalType === 'accountPoolDetail') {
      params.setPoolDirectAccountId(contact.account_id);
    }
    params.setDirectMessageForm({
      target_peer_id: contact.username ? `@${contact.username}` : contact.peer_id,
      target_display: contact.display_name,
      content: '',
    });
    params.setAccountDetailTab('云联系人');
  }

  async function createDirectMessageTask() {
    if (!params.accountDetail && !params.accountPoolDetail) return;
    params.setBusy('创建私发任务');
    const path = params.accountPoolDetail
      ? `/account-pools/${params.accountPoolDetail.pool.id}/direct-message-tasks`
      : `/tg-accounts/${params.accountDetail?.account.id}/direct-message-tasks`;
    await api<MessageTask>(path, {
      method: 'POST',
      body: JSON.stringify({
        ...params.directMessageForm,
        account_id: params.accountPoolDetail ? params.poolDirectAccountId || null : params.accountDetail?.account.id,
        target_display: params.directMessageForm.target_display || params.directMessageForm.target_peer_id,
        message_type: '文本',
      }),
    });
    params.showResult('私发消息已提交', '系统会按账号状态发送，可在账号发送记录中查看结果。');
    params.setDirectMessageForm({ target_peer_id: '', target_display: '', content: '' });
    await params.refresh();
    if (params.accountDetail) await params.refreshAccountDetail();
    if (params.accountPoolDetail) await params.refreshAccountPoolDetail();
    params.setBusy('');
  }

  async function createMessageSendTask(payload: MessageSendTaskCreate | MessageSendBatchCreate) {
    params.setBusy('创建消息发送任务');
    try {
      const isBatch = 'targets' in payload;
      const result = await api<MessageTask | MessageTask[]>('/message-send-tasks' + (isBatch ? '/batch' : ''), {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      const created = Array.isArray(result) ? result : [result];
      params.setTasks((current) => [...created, ...current.filter((item) => !created.some((task) => task.id === item.id))]);
      await params.refresh();
      return created;
    } finally {
      params.setBusy('');
    }
  }

  async function cancelTask(task: MessageTask) {
    params.setBusy('取消任务');
    const updated = await api<MessageTask>(`/message-send-tasks/${task.id}/cancel`, {
      method: 'POST',
      body: JSON.stringify({ actor: '普通用户' }),
    });
    params.showResult('发送明细已取消', `发送明细 #${updated.id} 已取消，不会继续发送。`);
    await params.refresh();
    params.setBusy('');
  }

  async function dispatchTask(task: MessageTask) {
    params.setBusy('派发消息');
    const result = await api<MessageTask>(`/message-send-tasks/${task.id}/dispatch`, { method: 'POST' });
    params.showResult('调度完成', result.status === '已发送' ? '消息已发送并记录回执。' : `发送失败：${result.failure_type}`);
    await params.refresh();
  }

  async function drainQueue() {
    params.setBusy('处理到期发送');
    const result = await api<{ processed: number }>('/worker/drain-once', { method: 'POST', body: JSON.stringify({ reason: '手动处理到期发送' }) });
    params.showResult('到期发送已处理', `本次已处理 ${result.processed} 条到期任务。`);
    await params.refresh();
  }

  async function retryTask(task: MessageTask) {
    params.setBusy('重试任务');
    const result = await api<MessageTask>(`/message-send-tasks/${task.id}/retry`, {
      method: 'POST',
      body: JSON.stringify({ actor: '普通用户', dispatch_now: true }),
    });
    params.showResult('重试完成', result.status === '已发送' ? '重试成功，消息已发送。' : `重试结果：${result.status}`);
    await params.refresh();
  }

  return {
    startDirectMessageToContact,
    createDirectMessageTask,
    createMessageSendTask,
    cancelTask,
    dispatchTask,
    drainQueue,
    retryTask,
  };
}
