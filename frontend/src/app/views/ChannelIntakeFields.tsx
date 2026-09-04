import React from 'react';
import { Form, InputNumber, Select } from 'antd';

export function ChannelIntakeFields() {
  return <div className="form-grid">
    <Form.Item name="initial_historical_post_limit" label="首次历史帖子数" initialValue={5}
      extra="最多 10 个来源；0 表示只接新帖。相册按一个来源计，首次范围冻结后不会随重启重抽。">
      <InputNumber min={0} max={10} />
    </Form.Item>
    <Form.Item name="source_expectation_mode" label="来源预期" initialValue="continuous_event_driven">
      <Select options={[
        { value: 'continuous_event_driven', label: '持续监听：无新帖为无机会' },
        { value: 'finite_existing_sources', label: '有限来源：空集合为来源不足' },
        { value: 'promised_daily_sources', label: '每天应有来源：缺失单独记录' },
      ]} />
    </Form.Item>
  </div>;
}
