import { ApiError } from '../../shared/api/client';
import type { ConfirmPayload, ModalState } from '../types';

interface ModalStateParams {
  message: { success: (content: string) => void };
  modalApi: {
    error: (config: { title: string; content: string }) => unknown;
    info: (config: { title: string; content: string }) => unknown;
    confirm: (config: {
      title: string;
      content: string;
      okText: string;
      cancelText: string;
      okButtonProps?: { danger: boolean };
      centered: boolean;
      onOk: () => Promise<void>;
    }) => void;
  };
  setModal: (modal: ModalState) => void;
}

export function createModalStateActions({ message, modalApi, setModal }: ModalStateParams) {
  function showResult(title: string, detail: string) {
    const content = title === detail ? title : `${title}：${detail}`;
    const combined = `${title} ${detail}`;
    if (/失败|异常|错误/.test(combined)) {
      void modalApi.error({ title, content: detail });
      return;
    }
    if (/请先|需要先/.test(combined)) {
      void modalApi.info({ title, content: detail });
      return;
    }
    void message.success(content);
  }

  function errorMessage(error: unknown) {
    if (error instanceof ApiError) {
      try {
        const parsed = JSON.parse(error.body) as { detail?: unknown };
        if (typeof parsed.detail === 'string') return parsed.detail;
      } catch {
        // Fall back to the raw body below.
      }
      return error.body || error.message;
    }
    return error instanceof Error ? error.message : String(error);
  }

  function handleActionError(error: unknown) {
    showResult('操作失败', errorMessage(error));
  }

  function closeModal() {
    setModal(null);
  }

  function openConfirm(payload: ConfirmPayload) {
    void modalApi.confirm({
      title: payload.title,
      content: payload.message,
      okText: payload.confirmLabel ?? '确认',
      cancelText: '取消',
      okButtonProps: payload.tone === 'danger' ? { danger: true } : undefined,
      centered: true,
      onOk: async () => {
        await payload.onConfirm();
        if (payload.restoreModalType) {
          setModal({ type: payload.restoreModalType });
        }
      },
    });
  }

  return {
    showResult,
    errorMessage,
    handleActionError,
    closeModal,
    openConfirm,
  };
}
