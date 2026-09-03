from __future__ import annotations

import json
from datetime import datetime, timezone
from sqlalchemy import text
from app.database import SessionLocal


def main():
    with SessionLocal() as session:
        # 1. Channel comment tasks overview
        tasks = list(
            session.execute(
                text("""
                    SELECT t.id,
                           t.name,
                           t.status,
                           t.type_config->>'target_comments_per_message' AS target_comments,
                           t.type_config->>'reply_min_per_message' AS reply_min,
                           t.type_config->>'rolling_window_days' AS rolling_days,
                           t.type_config->>'allow_returning_accounts' AS allow_returning,
                           t.pacing_config->>'multi_day_rampup' AS multi_day_rampup,
                           t.account_config->>'selection_mode' AS selection_mode,
                           t.next_run_at,
                           t.last_error,
                           t.task_lifecycle_epoch,
                           t.created_at,
                           t.updated_at,
                           ot.username AS channel_username,
                           ot.title AS channel_title
                    FROM tasks AS t
                    LEFT JOIN operation_targets AS ot ON ot.id = CAST(t.type_config->>'target_channel_id' AS INTEGER)
                    WHERE t.type = 'channel_comment' AND t.deleted_at IS NULL
                    ORDER BY t.status ASC, t.updated_at DESC
                """)
            ).mappings()
        )

        # 2. Obligations breakdown for each task
        obligations = list(
            session.execute(
                text("""
                    SELECT o.task_id,
                           t.name AS task_name,
                           o.status,
                           COUNT(*) AS count
                    FROM comment_fulfillment_obligations AS o
                    JOIN tasks AS t ON t.id = o.task_id
                    WHERE t.deleted_at IS NULL
                    GROUP BY o.task_id, t.name, o.status
                    ORDER BY t.name, o.status
                """)
            ).mappings()
        )

        # 3. Actions breakdown for each task
        actions_breakdown = list(
            session.execute(
                text("""
                    SELECT a.task_id,
                           t.name AS task_name,
                           a.status,
                           COUNT(*) AS count,
                           COUNT(DISTINCT a.account_id) AS distinct_accounts,
                           MAX(a.executed_at) AS latest_executed_at
                    FROM actions AS a
                    JOIN tasks AS t ON t.id = a.task_id
                    WHERE a.action_type = 'post_comment'
                    GROUP BY a.task_id, t.name, a.status
                    ORDER BY t.name, a.status
                """)
            ).mappings()
        )

        # 4. Success comments today
        today_comments = list(
            session.execute(
                text("""
                    SELECT t.id AS task_id,
                           t.name AS task_name,
                           COUNT(CASE WHEN a.status IN ('success', 'confirmed') AND (a.scheduled_at >= CURRENT_DATE OR a.executed_at >= CURRENT_DATE) THEN 1 END) AS today_success_count,
                           COUNT(CASE WHEN a.status = 'pending' THEN 1 END) AS pending_count,
                           COUNT(DISTINCT CASE WHEN a.status IN ('success', 'confirmed') AND (a.scheduled_at >= CURRENT_DATE OR a.executed_at >= CURRENT_DATE) THEN a.account_id END) AS today_distinct_senders,
                           MAX(CASE WHEN a.status IN ('success', 'confirmed') THEN a.executed_at END) AS latest_success_at
                    FROM tasks AS t
                    LEFT JOIN actions AS a ON a.task_id = t.id AND a.action_type = 'post_comment'
                    WHERE t.type = 'channel_comment' AND t.deleted_at IS NULL
                    GROUP BY t.id, t.name
                    ORDER BY t.name
                """)
            ).mappings()
        )

        # 5. Recent 20 action execution attempts for comments (successes and failures)
        recent_attempts = list(
            session.execute(
                text("""
                    SELECT ea.id AS attempt_id,
                           t.name AS task_name,
                           ea.action_id,
                           ea.status,
                           ea.failure_type,
                           ea.failure_detail,
                           ea.remote_message_id,
                           ea.created_at,
                           acc.phone_masked,
                           acc.tg_first_name
                    FROM execution_attempts AS ea
                    JOIN actions AS a ON a.id = ea.action_id
                    JOIN tasks AS t ON t.id = a.task_id
                    LEFT JOIN tg_accounts AS acc ON acc.id = a.account_id
                    WHERE a.action_type = 'post_comment'
                    ORDER BY ea.created_at DESC
                    LIMIT 20
                """)
            ).mappings()
        )

        # 6. Latest 15 successfully posted comments
        latest_comments = list(
            session.execute(
                text("""
                    SELECT a.id AS action_id,
                           t.name AS task_name,
                           a.account_id,
                           acc.phone_masked,
                           acc.tg_first_name,
                           a.payload->>'message_text' AS comment_text,
                           a.payload->>'channel_message_id' AS channel_message_id,
                           a.payload->>'reply_to_message_id' AS reply_to_id,
                           a.executed_at,
                           a.scheduled_at
                    FROM actions AS a
                    JOIN tasks AS t ON t.id = a.task_id
                    LEFT JOIN tg_accounts AS acc ON acc.id = a.account_id
                    WHERE a.action_type = 'post_comment'
                      AND a.status IN ('success', 'confirmed')
                    ORDER BY a.executed_at DESC NULLS LAST, a.created_at DESC
                    LIMIT 15
                """)
            ).mappings()
        )

        # 7. Next 15 scheduled pending comment actions
        next_pending = list(
            session.execute(
                text("""
                    SELECT a.id AS action_id,
                           t.name AS task_name,
                           a.account_id,
                           acc.phone_masked,
                           acc.tg_first_name,
                           a.scheduled_at,
                           a.payload->>'channel_message_id' AS channel_message_id,
                           a.payload->>'reply_to_message_id' AS reply_to_id
                    FROM actions AS a
                    JOIN tasks AS t ON t.id = a.task_id
                    LEFT JOIN tg_accounts AS acc ON acc.id = a.account_id
                    WHERE a.action_type = 'post_comment'
                      AND a.status = 'pending'
                    ORDER BY a.scheduled_at ASC
                    LIMIT 15
                """)
            ).mappings()
        )

        # 8. Channel messages being targeted
        messages = list(
            session.execute(
                text("""
                    SELECT cm.id,
                           cm.channel_target_id,
                           ot.title AS channel_title,
                           cm.message_id,
                           cm.message_url,
                           cm.content_preview,
                           cm.comment_available,
                           cm.published_at,
                           cm.created_at
                    FROM channel_messages AS cm
                    JOIN operation_targets AS ot ON ot.id = cm.channel_target_id
                    ORDER BY cm.created_at DESC
                    LIMIT 20
                """)
            ).mappings()
        )

        report = {
            "tasks": [dict(r) for r in tasks],
            "obligations": [dict(r) for r in obligations],
            "actions_breakdown": [dict(r) for r in actions_breakdown],
            "today_comments": [dict(r) for r in today_comments],
            "recent_attempts": [dict(r) for r in recent_attempts],
            "latest_success_comments": [dict(r) for r in latest_comments],
            "next_pending_comments": [dict(r) for r in next_pending],
            "channel_messages": [dict(r) for r in messages],
        }

        print("CHANNEL_COMMENT_INSPECTION_REPORT=" + json.dumps(report, ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    main()
