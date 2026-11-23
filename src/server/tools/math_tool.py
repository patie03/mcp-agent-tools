"""Deterministic math evaluator built on Python's ``ast`` module.

There is no eval() or exec() anywhere. The expression is parsed into a syntax tree and
walked node by node; only numbers, + - * / // % **, unary +/-, parentheses and a short list
of math functions are allowed. Anything else (names like __import__, attribute access,
strings, lists, lambdas, comprehensions) raises UnsafeExpressionError before any code runs.
"""

from __future__ import annotations

import ast
import math
import operator
from typing import Callable

MAX_EXPRESSION_CHARS = 200
MAX_EXPONENT = 1000
MAX_RESULT = 1e100

BIN_OPS: dict[type, Callable] = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
UNARY_OPS: dict[type, Callable] = {ast.UAdd: operator.pos, ast.USub: operator.neg}
FUNCTIONS: dict[str, Callable] = {
    "sqrt": math.sqrt, "abs": abs, "round": round, "floor": math.floor, "ceil": math.ceil,
    "log": math.log, "log10": math.log10, "exp": math.exp, "sin": math.sin, "cos": math.cos,
    "tan": math.tan, "min": min, "max": max,
}
CONSTANTS = {"pi": math.pi, "e": math.e}


class UnsafeExpressionError(ValueError):
    """The expression contains syntax that is not on the allow-list."""


def _check_size(value: float) -> float:
    if isinstance(value, complex) or abs(value) > MAX_RESULT:
        raise ValueError("Result is too large.")
    return value


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise UnsafeExpressionError(f"Only numbers are allowed, got {type(node.value).__name__}.")
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in BIN_OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > MAX_EXPONENT or abs(left) > MAX_RESULT):
            raise ValueError(f"Exponent is too large (max {MAX_EXPONENT}).")
        if isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)) and right == 0:
            raise ValueError("Division by zero.")
        return _check_size(BIN_OPS[type(node.op)](left, right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in UNARY_OPS:
        return UNARY_OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Name) and node.id in CONSTANTS:
        return CONSTANTS[node.id]
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in FUNCTIONS and not node.keywords):
        args = [_eval(a) for a in node.args]
        try:
            return _check_size(FUNCTIONS[node.func.id](*args))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Bad arguments for {node.func.id}(): {exc}") from None
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name):
            raise UnsafeExpressionError(f"Function '{node.func.id}' is not allowed.")
        if isinstance(node.func, ast.Attribute):
            raise UnsafeExpressionError(f"Method call '.{node.func.attr}()' is not allowed (attribute access is blocked).")
        raise UnsafeExpressionError("This kind of call is not allowed.")
    if isinstance(node, ast.Name):
        raise UnsafeExpressionError(f"Name '{node.id}' is not allowed.")
    if isinstance(node, ast.Attribute):
        raise UnsafeExpressionError("Attribute access (.) is not allowed.")
    raise UnsafeExpressionError(f"'{type(node).__name__}' is not allowed in a math expression.")


def safe_evaluate(expression: str) -> dict:
    """Evaluate a math expression safely and return {expression, result}."""
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("Empty expression.")
    expr = expression.strip()
    if len(expr) > MAX_EXPRESSION_CHARS:
        raise ValueError(f"Expression is too long (max {MAX_EXPRESSION_CHARS} characters).")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        raise UnsafeExpressionError("Not a valid math expression.") from None
    result = _eval(tree)
    if isinstance(result, float):
        result = round(result, 10)
        if result.is_integer() and abs(result) < 1e15:
            result = int(result)
    return {"expression": expr, "result": result}
