"""Parse an entire text challenge and compute it without partial-answer fallbacks."""
from __future__ import annotations

import ast
import operator
import re
import unicodedata
from fractions import Fraction


MAX_ARITHMETIC_ANSWER = 999999
CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
             "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CN_UNITS = {"十": 10, "百": 100, "千": 1000}
CN_SECTION_UNIT = 10000
CN_NUMBER_CHARS = "".join(CN_DIGITS) + "".join(CN_UNITS) + "万"
CN_NUMBER_PATTERN = re.compile(f"[{CN_NUMBER_CHARS}]+")
EXPRESSION_PATTERN = re.compile(rf"[0-9{CN_NUMBER_CHARS}.()+*/%^\- \t]+")
ARITHMETIC_OPERATOR_PATTERN = re.compile(r"[+*/%^\-]")
OPERATORS = {ast.Add: operator.add, ast.Sub: operator.sub,
             ast.Mult: operator.mul, ast.Div: operator.truediv}
UNARY_OPERATORS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
OPERATOR_WORDS = {"乘以": "*", "除以": "/", "乘": "*", "除": "/", "加": "+", "减": "-"}
OPERATOR_WORD_PATTERN = re.compile("|".join(OPERATOR_WORDS))
SYMBOL_TRANSLATION = str.maketrans({"×": "*", "x": "*", "X": "*", "÷": "/", "−": "-"})


def _number_value(raw: str) -> int | None:
    raw = raw.strip()
    if re.fullmatch(r"-?[0-9]+", raw):
        return int(raw)
    if not raw or any(char not in CN_NUMBER_CHARS for char in raw):
        return None
    if "万" not in raw:
        return _chinese_section(raw)
    if raw.count("万") != 1:
        return None
    high, low = raw.split("万")
    high_value = _chinese_section(high) if high else 1
    low_value = _chinese_section(low) if low else 0
    if high_value is None or low_value is None:
        return None
    return high_value * CN_SECTION_UNIT + low_value


def _chinese_section(raw: str) -> int | None:
    if all(char in CN_DIGITS for char in raw):
        return int("".join(str(CN_DIGITS[char]) for char in raw)) if raw else None
    total, previous_unit = 0, CN_SECTION_UNIT
    digit = None
    for char in raw:
        if char in CN_DIGITS:
            if digit not in (None, 0):
                return None
            digit = CN_DIGITS[char]
            continue
        unit = CN_UNITS.get(char)
        if unit is None or unit >= previous_unit or digit == 0:
            return None
        total += (1 if digit is None else digit) * unit
        previous_unit, digit = unit, None
    return total + (digit or 0)


def _extract_arithmetic_expression(text: str) -> str | None:
    normalized = unicodedata.normalize("NFKC", text or "").translate(SYMBOL_TRANSLATION)
    normalized = OPERATOR_WORD_PATTERN.sub(lambda match: OPERATOR_WORDS[match.group()], normalized)
    for match in EXPRESSION_PATTERN.finditer(normalized):
        expression = match.group().strip()
        if not re.search(rf"[0-9{CN_NUMBER_CHARS}]", expression):
            continue
        if not ARITHMETIC_OPERATOR_PATTERN.search(expression.lstrip("+-")):
            continue
        before = normalized[match.start() - 1:match.start()] if match.start() else ""
        after = normalized[match.end():match.end() + 1]
        if any(re.fullmatch(r"[a-zA-Z_]", char) for char in (before, after)):
            return before + expression + after
        return expression
    return None


def _replace_chinese_number(match: re.Match) -> str:
    value = _number_value(match.group())
    if value is None:
        raise ValueError("invalid_chinese_number")
    return str(value)


def _safe_eval_arithmetic_expr(expr: str) -> int | None:
    try:
        expression = CN_NUMBER_PATTERN.sub(_replace_chinese_number, expr.strip())
        node = ast.parse(expression, mode="eval")
        value = _evaluate_node(node.body, expression)
        return value.numerator if value.denominator == 1 else None
    except (SyntaxError, ValueError, ZeroDivisionError):
        return None


def _evaluate_node(node: ast.AST, expression: str) -> Fraction:
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        literal = ast.get_source_segment(expression, node)
        if literal and re.fullmatch(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", literal):
            return Fraction(literal)
    if isinstance(node, ast.UnaryOp) and type(node.op) in UNARY_OPERATORS:
        return UNARY_OPERATORS[type(node.op)](_evaluate_node(node.operand, expression))
    if isinstance(node, ast.BinOp) and type(node.op) in OPERATORS:
        return OPERATORS[type(node.op)](
            _evaluate_node(node.left, expression), _evaluate_node(node.right, expression),
        )
    raise ValueError("unsupported_arithmetic_expression")


def _arithmetic_answer(text: str) -> str:
    expression = _extract_arithmetic_expression(text)
    if expression is None:
        return ""
    answer = _safe_eval_arithmetic_expr(expression)
    if answer is None or abs(answer) > MAX_ARITHMETIC_ANSWER:
        return ""
    return str(answer)
