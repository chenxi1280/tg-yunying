import React from 'react';

export function useActionRunner(setBusy: (value: string) => void) {
  const [pendingActionKeys, setPendingActionKeys] = React.useState<string[]>([]);
  const busyRequestSeq = React.useRef(0);

  const isActionPending = React.useCallback((key: string) => pendingActionKeys.includes(key), [pendingActionKeys]);

  const runWithLoading = React.useCallback(async <T,>(key: string, busyLabel: string, task: () => Promise<T>): Promise<T> => {
    const requestSeq = busyRequestSeq.current + 1;
    busyRequestSeq.current = requestSeq;
    setPendingActionKeys((current) => [...current, key]);
    setBusy(busyLabel);
    try {
      return await task();
    } finally {
      setPendingActionKeys((current) => {
        const index = current.indexOf(key);
        if (index < 0) return current;
        const next = [...current];
        next.splice(index, 1);
        return next;
      });
      if (busyRequestSeq.current === requestSeq) setBusy('');
    }
  }, [setBusy]);

  return { pendingActionKeys, isActionPending, runWithLoading };
}
