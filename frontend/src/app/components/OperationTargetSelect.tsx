import React from 'react';
import { Select } from 'antd';
import type { SelectProps } from 'antd';
import { useOperationTargetOptions } from '../hooks/useOperationTargetOptions';
import type { OperationTarget, OperationTargetOptionQuery } from '../types/operations';

type OperationTargetSelectValue = number | number[];

export type OperationTargetSelectProps = Readonly<
  Omit<
    SelectProps<OperationTargetSelectValue>,
    'filterOption' | 'loading' | 'mode' | 'notFoundContent' | 'onSearch' | 'options' | 'showSearch' | 'status' | 'value'
  > & {
    query?: Omit<OperationTargetOptionQuery, 'ids'>;
    mode?: 'multiple';
    value?: OperationTargetSelectValue;
    status?: SelectProps<OperationTargetSelectValue>['status'];
    onTargetsLoaded?: (targets: readonly OperationTarget[]) => void;
  }
>;

function selectedTargetIds(value: OperationTargetSelectValue | undefined): readonly number[] {
  if (Array.isArray(value)) return value;
  return value === undefined ? [] : [value];
}

function targetLabel(target: OperationTarget): string {
  const username = target.username ? ` @${target.username}` : '';
  return `${target.title || `运营目标 #${target.id}`}${username}`;
}

export default function OperationTargetSelect({
  query = {},
  mode,
  value,
  status,
  onTargetsLoaded,
  ...selectProps
}: OperationTargetSelectProps) {
  const selectedIds = selectedTargetIds(value);
  const { targets, loading, error, search } = useOperationTargetOptions({
    ...query,
    ids: selectedIds,
  });
  const onTargetsLoadedRef = React.useRef(onTargetsLoaded);
  const options = React.useMemo(
    () => targets.map((target) => ({ value: target.id, label: targetLabel(target) })),
    [targets],
  );
  const notFoundContent = loading
    ? '正在加载运营目标…'
    : error
      ? `运营目标加载失败：${error}`
      : '未找到运营目标';

  React.useEffect(() => {
    onTargetsLoadedRef.current = onTargetsLoaded;
  }, [onTargetsLoaded]);

  React.useEffect(() => {
    onTargetsLoadedRef.current?.(targets);
  }, [targets]);

  return (
    <>
      <Select<OperationTargetSelectValue>
        {...selectProps}
        mode={mode}
        value={value}
        options={options}
        showSearch
        filterOption={false}
        onSearch={search}
        loading={loading}
        status={error ? 'error' : status}
        notFoundContent={notFoundContent}
      />
      {error && <div role="alert">运营目标加载失败：{error}</div>}
    </>
  );
}
