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
                           t.type_config->>'target_channel_id' AS target_channel_id,
                           t.type_config->>'target_comments_per_message' AS target_comments,
                           t.type_config->>'reply_min_per_message' AS reply_min,
                           t.type_config->>'rolling_window_days' AS rolling_days,
                           t.type_config->>'allow_returning_accounts' AS allow_returning,
                           t.pacing_config->>'multi_day_rampup' AS multi_day_rampup,
                           t.account_config->>'selection_mode' AS selection_mode,
                           t.next_run_at,
                           t.last_error,
                           t.task_lifecycle_epoch,
                           t.stats,
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

        # 2. Discussion group bindings for targeted channels
        discussion_bindings = list(
            session.execute(
                text("""
                    SELECT b.id,
                           b.channel_target_id,
                           ot.username AS channel_username,
                           ot.title AS channel_title,
                           b.discussion_target_id,
                           dot.username AS discussion_username,
                           dot.title AS discussion_title,
                           b.status,
                           b.updated_at
                    FROM channel_discussion_group_bindings AS b
                    JOIN operation_targets AS ot ON ot.id = b.channel_target_id
                    LEFT JOIN operation_targets AS dot ON dot.id = b.discussion_target_id
                """)
            ).mappings()
        )

        # 3. Channel messages count by channel and comment_available
        channel_message_stats = list(
            session.execute(
                text("""
                    SELECT cm.channel_target_id,
                           ot.title AS channel_title,
                           ot.username AS channel_username,
                           COUNT(*) AS total_messages,
                           COUNT(CASE WHEN cm.comment_available THEN 1 END) AS commentable_messages,
                           MAX(cm.published_at) AS latest_published_at
                    FROM channel_messages AS cm
                    JOIN operation_targets AS ot ON ot.id = cm.channel_target_id
                    GROUP BY cm.channel_target_id, ot.title, ot.username
                    ORDER BY cm.channel_target_id
                """)
            ).mappings()
        )

        # 4. Membership actions created for channels / discussion groups (join_channel, join_group)
        membership_actions = list(
            session.execute(
                text("""
                    SELECT a.task_id,
                           t.name AS task_name,
                           a.action_type,
                           a.status,
                           COUNT(*) AS count,
                           COUNT(DISTINCT a.account_id) AS distinct_accounts
                    FROM actions AS a
                    JOIN tasks AS t ON t.id = a.task_id
                    WHERE t.type = 'channel_comment'
                      AND a.action_type IN ('join_channel', 'join_group')
                    GROUP BY a.task_id, t.name, a.action_type, a.status
                    ORDER BY t.name, a.action_type, a.status
                """)
            ).mappings()
        )

        # 5. Comment actions breakdown
        comment_actions = list(
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

        # 6. Today comments summary
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

        report = {
            "tasks": [dict(r) for r in tasks],
            "discussion_bindings": [dict(r) for r in discussion_bindings],
            "channel_message_stats": [dict(r) for r in channel_message_stats],
            "membership_actions": [dict(r) for r in membership_actions],
            "comment_actions": [dict(r) for r in comment_actions],
            "today_comments": [dict(r) for r in today_comments],
        }

        print("COMMENT_DIAGNOSTICS_COMPACT=" + json.dumps(report, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
