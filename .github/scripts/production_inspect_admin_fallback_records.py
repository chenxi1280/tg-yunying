from __future__ import annotations

import json
from sqlalchemy import text
from app.database import SessionLocal


def main():
    with SessionLocal() as session:
        print("=" * 80)
        print("【统一群管理员账号兜底链路调用记录排查】")
        print("=" * 80)

        tasks = ["郑州大学", "西安天上人间", "三亚", "天津一品楼"]
        for kw in tasks:
            print(f"\n>>> 任务: {kw} <<<")

            # 1. 查找 task_membership_admission_items 中的救援/审批记录
            items = list(
                session.execute(
                    text("""
                        SELECT mi.id, mi.account_id, mi.phase, mi.failure_type,
                               mi.failure_detail, mi.rescue_status, mi.rescue_failure_detail,
                               mi.permission_failure_count
                        FROM task_membership_admission_items mi
                        JOIN tasks t ON t.id = mi.task_id
                        WHERE t.name LIKE :kw
                          AND (mi.rescue_status != '' OR mi.rescue_failure_detail != '' OR mi.permission_failure_count > 0)
                        ORDER BY mi.updated_at DESC
                        LIMIT 10
                    """),
                    {"kw": f"%{kw}%"},
                ).mappings()
            )
            print(f"  [task_membership_admission_items] 带有救援/失败重试痕迹的条目数 (样例展示最多10条，共匹配到): {len(items)}")
            for item in items:
                print(f"    Acc {item['account_id']}: phase={item['phase']}, fail_type={item['failure_type']}, fail_det={item['failure_detail']}")
                print(f"      rescue_status={item['rescue_status']}, rescue_fail_det={item['rescue_failure_detail']}, perm_fail_cnt={item['permission_failure_count']}")

            # 2. 查找 actions 中的管理员兜底细节字段
            actions = list(
                session.execute(
                    text("""
                        SELECT a.id, a.action_type, a.account_id, a.status, a.scheduled_at, a.executed_at,
                               a.result->>'admin_restriction_lift_detail' as admin_lift,
                               a.result->>'join_request_approval_detail' as join_approval,
                               a.result->>'join_request_link_join_detail' as link_join,
                               a.result->>'error_message' as err_msg,
                               a.result->>'error_code' as err_code,
                               a.result->>'membership_status' as mem_status
                        FROM actions a
                        JOIN tasks t ON t.id = a.task_id
                        WHERE t.name LIKE :kw
                          AND (
                            a.result->>'admin_restriction_lift_detail' IS NOT NULL
                            OR a.result->>'join_request_approval_detail' IS NOT NULL
                            OR a.result->>'join_request_link_join_detail' IS NOT NULL
                            OR a.action_type = 'invite_group_account'
                          )
                        ORDER BY a.created_at DESC
                        LIMIT 10
                    """),
                    {"kw": f"%{kw}%"},
                ).mappings()
            )
            print(f"  [actions] 触发过管理员兜底/救援的 Actions: {len(actions)}")
            for a in actions:
                print(f"    Action {a['id'][:8]} | type={a['action_type']} | acc={a['account_id']} | status={a['status']}")
                print(f"      admin_lift={a['admin_lift']}")
                print(f"      join_approval={a['join_approval']}")
                print(f"      link_join={a['link_join']}")
                print(f"      err={a['err_code']}: {a['err_msg']}")

        # 3. 查看全局所有使用管理员 515 执行的 actions
        print("\n>>> 管理员账号 (ID: 515) 历史执行的所有 Actions 记录 <<<")
        admin_actions = list(
            session.execute(
                text("""
                    SELECT a.id, t.name as task_name, a.action_type, a.status,
                           a.scheduled_at, a.executed_at,
                           a.payload->>'target_account_id' as target_acc,
                           a.result->>'error_code' as err_code,
                           a.result->>'error_message' as err_msg,
                           a.result->>'rescue_status' as rescue_status,
                           a.result->>'detail' as detail
                    FROM actions a
                    LEFT JOIN tasks t ON t.id = a.task_id
                    WHERE a.account_id = 515
                    ORDER BY a.created_at DESC
                    LIMIT 20
                """)
            ).mappings()
        )
        print(f"  管理员账号 515 的 Actions 总数 (最近 20 条):")
        for aa in admin_actions:
            print(f"    Task: {aa['task_name']} | Action {aa['id'][:8]} | type={aa['action_type']} | status={aa['status']}")
            print(f"      target_acc={aa['target_acc']} | err={aa['err_code']} | msg={aa['err_msg']} | rescue_status={aa['rescue_status']} | detail={aa['detail']}")


if __name__ == "__main__":
    main()
