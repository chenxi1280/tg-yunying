# 2026-06-28 Handoff Supervision Upgrade

## 背景

用户在真实使用 tg-yunying 多 Agent 线程时发现协同断链：Agent 输出阶段结论后，没有继续发送目标线程消息，导致下游 Agent 没有被触发；product 线程还出现过在收到“实现”后直接改代码的问题。

## 历史断点

| issue | observed_behavior | protocol_gap | upgrade |
| --- | --- | --- | --- |
| Product 越权实现 | product 在用户要求实现后直接进入代码实现，而不是投递 dev | product 角色边界只写了职责，没有写硬性禁止和替代动作 | product 不能改代码；实现请求必须转成 Product Design Complete + dev handoff |
| 产品设计半成品 | product 只产出初稿或部分设计就说完成 | 没有设计完成自检闸门 | 新增 Product Design Complete，覆盖功能、前端、后端、数据流转、QA 和遗漏自检 |
| Dev 未触发 QA | dev 完成后未主动发送 QA 验收消息 | `notify_qa` 可被误当成完成 | Development Complete 必须包含 `qa_handoff_message_id` 和 `handoff_delivery_status` |
| QA 未触发产品验收 | QA 通过后未主动发送 product acceptance 消息 | `notify_product_acceptance=true` 没有真实投递证明 | Validation Report 必须包含 `product_acceptance_message_id` |
| 产品接受后流程停住 | product accepted 后 Release Gate / production verify 仍可能未投递 | `product_accepted` 被误用成闭环 | Product Acceptance 必须继续投递 dev/ops/prod-diagnosis |
| 主控无法监督 | 状态看板没有投递状态、ACK 截止和重试字段 | 只能靠聊天记忆判断断链 | `agent-status-board.md` 新增 handoff delivery 字段和 Flow Supervisor 规则 |

## 本次更新

- 更新 `/Users/xida/PycharmProjects/tg-yunying/AGENTS.md`，加入强制真实投递、Product Design Complete 和 Flow Supervisor。
- 更新 `README.md`，把协议从说明升级为可执行闭环规则。
- 更新 `agent-registry.md`，新增 `flow-supervisor`，并写明各 Agent 的投递硬条件。
- 更新 `agent-status-board.md`，新增 `handoff_required`、`handoff_delivery_status`、`target_thread`、`ack_deadline`、`retry_count`、`supervisor_action` 等字段。
- 更新 handoff、development complete、validation report、product acceptance 模板。
- 新增 Product Design Complete 和 Handoff Supervisor Check 模板。

## 新关闭条件

- 有 `next_agent` 时，当前 Agent 必须真实投递目标线程消息。
- 不能用 `notify_xxx=true` 代替真实投递。
- 无法投递时必须写 `requires_orchestrator_send=true`、完整待发送正文和阻塞原因。
- `handoff_delivery_status=pending/timeout` 时不能宣布阶段闭环。
- `flow-supervisor` 必须发现并重投断链消息，或显式标记 blocked。

## 状态

- document_flow_verified: E1
- thread_sync: partial_ack
- acknowledged_agents: product, qa, prod-diagnosis
- pending_agents: dev
- pending_reason: dev thread received the protocol update message, but no final ACK was returned during this supervisor check.
- production_effect: not_applicable
