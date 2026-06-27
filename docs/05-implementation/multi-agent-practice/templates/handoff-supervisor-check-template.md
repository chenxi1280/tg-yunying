# Handoff Supervisor Check Template

- check_id:
- from_agent: flow-supervisor
- checked_at:
- status_board:
- check_scope: all | intake | batch | bug | message
- result: clear | resent | blocked | escalated

## 检查项

| message_id | current_agent | next_agent | handoff_required | delivery_status | target_thread | ack_deadline | retry_count | finding | action |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |

## 断链判定

- `next_agent` 不为空，但 `handoff_required=false`。
- `handoff_required=true`，但 `handoff_delivery_status=pending/timeout`。
- `handoff_delivery_status=sent`，但超过 `ack_deadline` 仍无 `acknowledged`。
- `qa_pass` 后没有 product acceptance 消息。
- `failed` 后没有 dev rework 消息。
- `product_accepted` 后 Release Gate 或 production verify 未投递。
- L3 线上链路未回到 prod-diagnosis 做 E4 复核。

## 自动动作

- resend_handoff:
- new_handoff_message_id:
- target_thread_id:
- retry_count_after:
- supervisor_action:
- blocked_reason:

Flow Supervisor 只恢复交接和状态，不替代 product/dev/qa/prod-diagnosis 的专业结论。重发后必须更新 `agent-status-board.md`，并等待目标 Agent ACK。
