from app.models import VerificationTask
from app.integrations.telegram import SendResult
from app.services.verification import _apply_batch_approval_detail, _verification_send_failure_status


def test_batch_approval_detail_marks_blocked_tasks_once():
    task = VerificationTask(
        status="需人工处理",
        failure_detail="目标能力重查未通过：缓存频道不可访问 / 账号无权限。",
    )
    approval = ("需人工处理", "未找到可执行群验证放行的管理员账号", None)

    _apply_batch_approval_detail([task], approval)
    _apply_batch_approval_detail([task], approval)

    assert task.failure_detail == (
        "管理员放行：需人工处理（未找到可执行群验证放行的管理员账号）；"
        "目标能力重查未通过：缓存频道不可访问 / 账号无权限。"
    )


def test_batch_approval_detail_skips_restored_tasks():
    task = VerificationTask(
        status="已处理",
        failure_detail="目标能力重查通过：可发言。",
    )

    _apply_batch_approval_detail([task], ("已执行", "已点击 1 条通过（管理员）验证", 8))

    assert task.failure_detail == "目标能力重查通过：可发言。"


def test_verification_send_failure_status_supports_send_result():
    result = SendResult(False, failure_type="群无权限", detail="账号不可发言")

    assert _verification_send_failure_status(result) == "失败"
