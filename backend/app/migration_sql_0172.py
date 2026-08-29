VIEW_FACT_DATE_BACKFILL_SQL = """
UPDATE view_remote_facts vrf
SET obligation_local_date = COALESCE(
    (SELECT tdl.obligation_local_date
     FROM view_fulfillment_obligations vfo
     JOIN task_day_ledgers tdl ON tdl.id = vfo.task_day_ledger_id
     WHERE vfo.id = vrf.obligation_id),
    (vrf.created_at AT TIME ZONE 'Asia/Shanghai')::date,
    CURRENT_DATE
)
WHERE vrf.obligation_local_date IS NULL
"""

CONFIRMED_OWNER_BACKFILL_SQL = """
INSERT INTO channel_view_daily_identity_owners (
    id, tenant_id, target_peer_id, channel_message_id, account_id,
    obligation_local_date, state, logical_task_id, obligation_id, action_id,
    request_identity, version, created_at, updated_at
)
SELECT vrf.id, vrf.tenant_id, vrf.target_peer_id, vrf.channel_message_id,
       vrf.account_id, vrf.obligation_local_date, 'confirmed', tdl.task_id,
       vrf.obligation_id, vfo.current_action_id,
       tdl.task_id || ':' || vrf.obligation_id, 1, vrf.created_at, vrf.created_at
FROM view_remote_facts vrf
JOIN view_fulfillment_obligations vfo ON vfo.id = vrf.obligation_id
JOIN task_day_ledgers tdl ON tdl.id = vfo.task_day_ledger_id
ON CONFLICT (target_peer_id, channel_message_id, account_id, obligation_local_date)
DO NOTHING
"""

ACTIVE_OWNER_BACKFILL_SQL = """
INSERT INTO channel_view_daily_identity_owners (
    id, tenant_id, target_peer_id, channel_message_id, account_id,
    obligation_local_date, state, logical_task_id, obligation_id, action_id,
    request_identity, version, created_at, updated_at
)
SELECT vfo.id, vfo.tenant_id, ot.tg_peer_id, vfo.channel_message_id,
       vfo.account_id, tdl.obligation_local_date,
       CASE WHEN EXISTS (
           SELECT 1 FROM execution_attempts ea
           WHERE ea.action_id = a.id AND ea.gateway_call_started_at IS NOT NULL
       ) THEN 'unknown' ELSE 'pre_gateway' END,
       tdl.task_id, vfo.id, a.id, tdl.task_id || ':' || vfo.id,
       1, vfo.created_at, vfo.created_at
FROM view_fulfillment_obligations vfo
JOIN task_day_ledgers tdl ON tdl.id = vfo.task_day_ledger_id
JOIN actions a ON a.id = vfo.current_action_id
JOIN channel_messages cm ON cm.id = vfo.channel_message_id
JOIN operation_targets ot ON ot.id = cm.channel_target_id
WHERE vfo.status IN ('pending','unknown')
  AND a.status IN ('pending','claiming','executing','retryable_failed','unknown_after_send')
ON CONFLICT (target_peer_id, channel_message_id, account_id, obligation_local_date)
DO NOTHING
"""


__all__ = [
    "ACTIVE_OWNER_BACKFILL_SQL",
    "CONFIRMED_OWNER_BACKFILL_SQL",
    "VIEW_FACT_DATE_BACKFILL_SQL",
]
