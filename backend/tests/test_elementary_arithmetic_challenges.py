import pytest

from app.services.verification_arithmetic import (
    _arithmetic_answer,
    _safe_eval_arithmetic_expr,
    _number_value,
)
pytestmark = pytest.mark.no_postgres


def test_chinese_number_parsing():
    assert _number_value("零") == 0
    assert _number_value("五") == 5
    assert _number_value("十") == 10
    assert _number_value("十一") == 11
    assert _number_value("二十五") == 25
    assert _number_value("一百零八") == 108
    assert _number_value("一千二百三十四") == 1234
    assert _number_value("两千零二十五") == 2025
    assert _number_value("一万") == 10000


def test_elementary_arithmetic_safe_eval():
    assert _safe_eval_arithmetic_expr("3 + 5") == 8
    assert _safe_eval_arithmetic_expr("10 - 4") == 6
    assert _safe_eval_arithmetic_expr("6 * 7") == 42
    assert _safe_eval_arithmetic_expr("20 / 4") == 5
    assert _safe_eval_arithmetic_expr("3 + 5 * 2") == 13
    assert _safe_eval_arithmetic_expr("(10 - 4) * 2") == 12
    assert _safe_eval_arithmetic_expr("20 - 6 / 2") == 17
    # Division by zero
    assert _safe_eval_arithmetic_expr("10 / 0") is None
    # Invalid expressions
    assert _safe_eval_arithmetic_expr("__import__('os')") is None
    assert _safe_eval_arithmetic_expr("hello") is None


def test_arithmetic_answer_extraction():
    # Simple addition & subtraction
    assert _arithmetic_answer("3 + 5 = ?") == "8"
    assert _arithmetic_answer("12 - 7 =") == "5"
    # Multiplication & division
    assert _arithmetic_answer("6 * 7 = ?") == "42"
    assert _arithmetic_answer("8 × 9 = ?") == "72"
    assert _arithmetic_answer("7 x 8 = ?") == "56"
    assert _arithmetic_answer("20 ÷ 4 = ?") == "5"
    assert _arithmetic_answer("36 / 6 = ?") == "6"
    # Precedence
    assert _arithmetic_answer("请回答：3 + 5 * 2 等于多少") == "13"
    assert _arithmetic_answer("验证题目: 20 - 4 / 2 = ?") == "18"
    # Chinese operator & numbers
    assert _arithmetic_answer("入群验证：三加五等于多少") == "8"
    assert _arithmetic_answer("六乘以七等于几") == "42"
    assert _arithmetic_answer("二十四除以四等于多少") == "6"
    assert _arithmetic_answer("一百二十加三百八十等于多少") == "500"
    assert _arithmetic_answer("一千减二百五十") == "750"
    # No math
    assert _arithmetic_answer("欢迎加入本群，请文明交流") == ""


@pytest.mark.parametrize(("text", "expected"), [
    ("十万", 100000), ("十二万", 120000), ("十二万三千零四", 123004),
    ("一万零一", 10001), ("二零二六", 2026), ("一百百", None), ("", None),
])
def test_chinese_section_values(text, expected):
    assert _number_value(text) == expected


@pytest.mark.parametrize(("text", "expected"), [
    ("３＋５＝？", "8"), ("5 X 6=?", "30"), ("十万加一", "100001"),
    ("十二万减一千", "119000"), ("（三加五）乘以二", "16"),
    ("(10 - 4) * 2=?", "12"), ("(2+3)*(4+5)=?", "45"),
    ("2 * (3 + (4 * 5)) = ?", "46"), ("1.5 + 2.5 = ?", "4"),
    ("0.1 + 0.2 + 0.7=?", "1"), ("8 / 3 * 3=?", "8"),
    ("1/2 + 1/2 = ?", "1"), ("-2 * (-3 + 1)=?", "4"),
])
def test_complete_compound_arithmetic(text, expected):
    assert _arithmetic_answer(text) == expected


@pytest.mark.parametrize("text", [
    "(2+3", "2*(3+4))", "2**3+4", "3//2+1", "3/0+2", "1.5+2", "999999+1",
    "2+foo(3+4)", "一百百加一", "123 / 0", "1e3+2", "3+2abc", "2^3+4",
])
def test_invalid_expression_never_returns_a_partial_answer(text):
    assert _arithmetic_answer(text) == ""


@pytest.mark.parametrize("expression", ["__import__('os')", "True + 1", "hello", "1/0", "1e3+2"])
def test_safe_eval_rejects_non_arithmetic_or_unsupported_literals(expression):
    assert _safe_eval_arithmetic_expr(expression) is None


@pytest.mark.parametrize(("text", "expected"), [
    ("请输入 2 * (3 + (4 * 5)) = ?", "46"), ("请输入 1.5 + 2.5 = ?", "4"),
    ("请输入 十万加一", "100001"), ("验证码 654321", "654321"),
    ("请输入 123 / 0", None), ("请输入 123 * (2 + 3", None),
])
def test_auto_verification_submits_only_complete_valid_answer(monkeypatch, text, expected):
    from types import SimpleNamespace
    from app.integrations.telegram import OperationResult
    from app.models import VerificationTask
    from app.services import membership_challenges

    submitted, attempts = [], []
    task = VerificationTask(detected_reason=text, failure_detail="", target_peer_id="-1001")
    account = SimpleNamespace(id=1, session_ciphertext="test")
    context = {"messages": [{"text": text}]}
    monkeypatch.setattr(membership_challenges, "read_challenge_context_with_fallback",
                        lambda *args, **kwargs: SimpleNamespace(context=context))
    monkeypatch.setattr(membership_challenges, "record_challenge_attempt",
                        lambda *args, **kwargs: attempts.append(kwargs))

    def submit(_account, _peer, answer, *_args):
        submitted.append(answer)
        return OperationResult(True, "已处理")

    monkeypatch.setattr(membership_challenges.gateway, "submit_verification_response", submit)
    result = membership_challenges.auto_resolve_text_verification(None, task, account, object())
    assert submitted == ([] if expected is None else [expected])
    assert result.ok is (expected is not None)
    if expected is None:
        assert result.failure_type == "verification_answer_missing"
        assert task.status == "需人工处理"
        assert attempts[0]["status"] == "text_answer_missing"
