from __future__ import annotations

from importlib import import_module


def _lazy_call(module_name: str, function_name: str):
    def invoke(*args, **kwargs):
        module = import_module(module_name)
        return getattr(module, function_name)(*args, **kwargs)

    invoke.__name__ = function_name
    invoke.target_module = module_name
    invoke.target_function = function_name
    return invoke


drain_account_sync_records = _lazy_call("app.services.accounts", "drain_account_sync_records")
drain_profile_sync_records = _lazy_call("app.services.accounts", "drain_profile_sync_records")
drain_account_online_keepalive = _lazy_call(
    "app.services.account_online_state",
    "drain_account_online_keepalive",
)
drain_account_login_batches = _lazy_call(
    "app.services.account_login.drain",
    "drain_account_login_batches",
)
drain_account_login_reconciliation = _lazy_call(
    "app.services.account_login.reconciliation",
    "drain_account_login_reconciliation",
)
drain_account_post_login_initializations = _lazy_call(
    "app.services.account_post_login_init.drain",
    "drain_account_post_login_initializations",
)
drain_notification_outbox = _lazy_call(
    "app.services.account_login.notifications",
    "drain_notification_outbox",
)
drain_account_security_batches = _lazy_call(
    "app.services.account_security.service",
    "drain_account_security_batches",
)
drain_ai_message_memory_maintenance = _lazy_call(
    "app.services.task_center.ai_message_memory_maintenance",
    "drain_ai_message_memory_maintenance",
)
drain_archives = _lazy_call("app.services.archives", "drain_archives")
drain_continuous_campaigns = _lazy_call(
    "app.services.campaign_runs",
    "drain_continuous_campaigns",
)
drain_group_listeners = _lazy_call(
    "app.services.group_listeners",
    "drain_group_listeners",
)
drain_operation_tasks = _lazy_call(
    "app.services.operations",
    "drain_operation_tasks",
)
drain_task_center = _lazy_call("app.services.task_center.service", "drain_task_center")
drain_task_dispatcher = _lazy_call(
    "app.services.task_center.service",
    "drain_task_dispatcher",
)
drain_search_dispatcher = _lazy_call(
    "app.services.task_center.service",
    "drain_search_dispatcher",
)
drain_task_listener = _lazy_call(
    "app.services.task_center.service",
    "drain_task_listener",
)
drain_task_planner = _lazy_call(
    "app.services.task_center.service",
    "drain_task_planner",
)
drain_task_recovery = _lazy_call(
    "app.services.task_center.service",
    "drain_task_recovery",
)
drain_task_metrics = _lazy_call(
    "app.services.task_center.metrics_runtime",
    "drain_task_metrics",
)
drain_ai_generation = _lazy_call(
    "app.services.task_center.ai_generation_worker",
    "drain_ai_generation",
)
drain_comment_generation = _lazy_call(
    "app.services.task_center.comment_generation_worker",
    "drain_comment_generation",
)
drain_voice_profile_generation = _lazy_call(
    "app.services.task_center.account_voice_profile_generation_worker",
    "drain_voice_profile_generation",
)
drain_source_media_cache = _lazy_call(
    "app.services.source_media",
    "drain_source_media_cache",
)
drain_material_cache = _lazy_call(
    "app.services.material_cache",
    "drain_material_cache",
)
cleanup_temp_files = _lazy_call("app.services.temp_files", "cleanup_temp_files")
dispatch_task = _lazy_call("app.services.messages", "dispatch_task")
get_task_queue = _lazy_call("app.task_queue", "get_task_queue")
dispatcher_runtime_reservation_count = _lazy_call(
    "app.services.task_center.dispatcher",
    "dispatcher_runtime_reservation_count",
)
get_image_verification_runtime = _lazy_call(
    "app.services.image_verification_runtime",
    "get_image_verification_runtime",
)
