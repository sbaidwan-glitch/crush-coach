"""A small, safe calculator for the coach's `calculate` tool.

The model is bad at arithmetic, so in recommendation mode every number it derives
comes from here. It evaluates one arithmetic expression with `ast`, never `eval`: only
numbers, + - * / // % **, parentheses, a few constants and functions. Anything else
(names, attributes, calls to other functions, huge powers) is refused with a plain
message the model can read and fix.
"""

import ast
import math
import operator
from collections.abc import Callable
from typing import Any

MAX_EXPRESSION_CHARS = 200
MAX_POWER = 100  # an exponent bigger than this is refused: 9**9**9 would hang
MAX_MAGNITUDE = 1e15  # so is a base this large raised to a power

_BINARY: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY: dict[type, Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_CONSTANTS = {"pi": math.pi, "e": math.e}
_FUNCTIONS: dict[str, Callable[..., float]] = {
    "sqrt": math.sqrt,
    "sin": math.sin,  # radians
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "radians": math.radians,
    "degrees": math.degrees,
    "ln": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
}


class CalcError(ValueError):
    """The expression can't be evaluated. The message is safe to show to the model."""


def _number(node: ast.expr) -> float:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, int | float):
            raise CalcError("only numbers are allowed")
        return node.value
    if isinstance(node, ast.Name):
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        raise CalcError(f"unknown name {node.id!r}; write the number instead")
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_number(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        left, right = _number(node.left), _number(node.right)
        if isinstance(node.op, ast.Pow) and (
            abs(right) > MAX_POWER or abs(left) > MAX_MAGNITUDE
        ):
            raise CalcError("that power is too large")
        try:
            return _BINARY[type(node.op)](left, right)
        except ZeroDivisionError:
            raise CalcError("division by zero") from None
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _FUNCTIONS
        and not node.keywords
    ):
        args = [_number(arg) for arg in node.args]
        try:
            return _FUNCTIONS[node.func.id](*args)
        except (ValueError, TypeError, OverflowError) as exc:
            raise CalcError(
                f"{node.func.id}() can't take those numbers ({exc})"
            ) from None
    raise CalcError(
        "only arithmetic with numbers, pi, e and sqrt/sin/cos/... is allowed"
    )


def evaluate(expression: str) -> float:
    """The value of one arithmetic expression. Raises `CalcError` if it isn't one."""
    text = (expression or "").strip().replace("^", "**").replace("×", "*")
    if not text:
        raise CalcError("the expression is empty")
    if len(text) > MAX_EXPRESSION_CHARS:
        raise CalcError("the expression is too long")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        raise CalcError("that is not a valid arithmetic expression") from None
    value = _number(tree.body)
    if isinstance(value, complex) or math.isnan(value) or math.isinf(value):
        raise CalcError("the result is not a finite number")
    return value


def format_result(value: float) -> str:
    """The number as a short string: about 4 significant figures, no trailing zeros."""
    if value == 0:
        return "0"
    if abs(value) >= 1e6 or abs(value) < 1e-4:
        return f"{value:.4g}"
    text = f"{value:.4f}" if abs(value) < 1000 else f"{value:.2f}"
    return text.rstrip("0").rstrip(".")
